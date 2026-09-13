"""Governance plane on Postgres: erasure keys, tenant identity, tenant config.

Everything here is state that decides whether other state is *readable* or
*allowed*, so the porting notes below are correctness properties of running two
replicas against one store rather than incidental engine differences.

1. **The crypto-shred key must converge — this is the hazard that dominates the
   file.**  ``get_or_create_governance_key`` generates a random 256-bit DEK when a
   scope has none.  On SQLite the database-wide write lock made "check, then
   create" indivisible.  Under two replicas a bare check-then-insert lets both
   generate a DEK, both seal content with their own, and only one wrapped DEK
   reach the table — every field the loser sealed is then unreadable *forever*,
   because the KEK cannot regenerate a random DEK.  That is silent, permanent
   data loss that looks like success at write time.  So the insert itself is the
   election: ``INSERT ... ON CONFLICT (scope_key, subject_key) DO NOTHING
   RETURNING``.  A row came back ⇒ our DEK is the persisted one and we may return
   it.  No row came back ⇒ we lost, and the *winner's* row is re-read and
   unwrapped; the DEK we generated is discarded unused.  A generated-but-not-
   persisted DEK is never returned on any path.  The same election covers the
   formation-contract signing key, where a discarded key would produce
   attestations that verify against a private key nobody holds.

2. **Shredding is atomic and fails closed (DW-007).**  ``shred_governance_key``
   takes the row ``FOR UPDATE``, confirms the key exists under that lock, and only
   then zeroes ``wrapped_dek`` and stamps ``shredded_at``, so a concurrent
   ``get_or_create_governance_key`` either completes before the erase or observes
   the erased row and refuses.  There is no window in which the scope is
   simultaneously shredded and still handing out key material.  Re-shredding is
   idempotent and returns ``True`` with a refreshed stamp, exactly as SQLite did.

3. **"Exactly one active version" is a multi-row invariant, so it is one
   statement.**  SQLite wrote ``UPDATE ... SET active = 0`` and then inserted the
   new active row; two concurrent saves interleaved on Postgres would leave two
   rows active and the read model would pick by timestamp.  ``save_tenant_prompt_version``
   and ``save_project_memory_config`` serialise on a transaction-scoped advisory
   lock keyed by tenant (the tenant may have no rows yet, so ``FOR UPDATE`` alone
   locks nothing), allocate the next version in SQL, and re-establish the flag
   with a single ``UPDATE ... SET active = (version = %s)``.

4. **Tenant-agent registration converges on one identity.**  The refresh path is
   an ``INSERT ... ON CONFLICT (tenant_id, agent_id) DO UPDATE`` guarded by
   ``name_key``, so a re-registration advances ``last_seen_at`` and preserves
   ``created_at`` (and the stored ``agent_id`` spelling, name, and source) while a
   name collision refuses instead of overwriting.  See
   :meth:`GovernancePlaneMixin.register_tenant_agent` for the uniqueness caveat:
   the SQLite schema carried *unique* indexes on ``(tenant_id, agent_id_key)`` and
   ``(tenant_id, name_key)``; the migrated schema has those two indexes but
   non-unique, so the case-insensitive-id and duplicate-name invariants are held
   by the advisory lock here rather than by the database.

5. **Filters are pushed into SQL.**  Tenant reads seek ``tenant_agents_tenant_idx``
   / ``tenant_agents_id_key_idx`` / ``tenant_agents_name_key_idx`` /
   ``agent_motive_assignments_tenant_idx`` / ``tenant_prompt_versions_tenant_idx``
   / ``project_memory_config_versions_tenant_idx``, and ``purge_tenant_state``
   selects the episodes and decisions it must touch by jsonb scope predicate
   instead of validating every stored row to compare one string.

``override`` and ``config`` are real ``jsonb`` and decode to Python objects on
read; ``active`` is a real ``boolean``; timestamps stay ISO-8601 text written by
``datetime_to_text`` so ordering and receipts remain byte-comparable with SQLite.
Text ordering carries ``COLLATE "C"`` wherever SQLite's BINARY collation decided a
tie-break, so both engines return rows in the same order.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from memotron.crypto import (
    CONTENT_KEY_BYTES,
    ContentKeyUnavailableError,
    is_sealed_content,
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
from memotron.models import FormationContractAttestation, ScopeKind
from memotron.storage._shared import (
    tenant_agent_from_row,
    tenant_prompt_version_from_row,
)
from memotron.storage._shared._governance import (
    formation_signer_id,
    llm_credential_is_readable,
    resolve_embedding_endpoint,
)
from memotron.storage.postgres._common import as_json
from memotron.storage.postgres._engine import datetime_to_text, json_dumps

# Recorded on every provisioned key and surfaced on the erasure certificate.
# Spelled exactly as the SQLite backend spelled it: the parity suite and stored
# certificates compare the string.
_KEY_ALGORITHM = "AES-256-GCM envelope; HMAC-SHA256 commitments"

# The subject under which a tenant's LLM credential DEK is filed.  Scope key is
# ``tenant:<tenant_id>``; both halves are contract with WS-12.
_LLM_CREDENTIAL_SUBJECT = "llm_credentials"

# Seed for tenant-scoped advisory locks ("DWTN").  Arbitrary but fixed; it only
# has to keep these locks from colliding with the migration lock and the policy
# plane's alias locks.
_TENANT_LOCK_SEED = 0x44_57_54_4E

# Providers the interface accepts, spelled once.  Order matters only for the
# error message, which is contract.  ``anthropic`` is retired: Memotron
# reaches Claude through the JedAI Gateway, so it is rejected with a specific
# migration message rather than accepted (parity with the SQLite substrate).
_LLM_PROVIDERS = frozenset({"openai", "litellm"})

# The generated-version ordinal.  ``save_*`` allocates ``<prefix>-v<n>`` with a
# monotonically increasing ``n``, so the numeric suffix is the insertion order —
# which is what SQLite's ``rowid`` tie-break on ``active_project_memory_config``
# actually meant.  A row whose version does not match the generated shape sorts
# as 0, which is where SQLite's rowid would have put a hand-written row anyway.
_PROJECT_VERSION_ORDINAL = "CASE WHEN version ~ '^project-v[0-9]+$' THEN substring(version from 10)::bigint ELSE 0 END"

_TENANT_PROMPT_COLUMNS = """
    tenant_id, version, prompt_text, motive_name, source_profile,
    source_profile_version, active, created_at, updated_at
"""

_PROJECT_CONFIG_COLUMNS = """
    tenant_id, version, config, configured_by, active, created_at, updated_at
"""

# Scope-key membership test for a payload carrying a nested ``MemoryScope``.
# ``MemoryScope.key`` is a property, never a serialised field, so the two scope
# terms are matched as a pair against the unnested key list.  A payload with no
# scope yields NULLs and matches nothing, which is what the Python
# ``record.scope is not None`` guard did.
_SCOPE_PAIR_PREDICATE = """
(payload->'{member}'->>'kind', payload->'{member}'->>'scope_id')
    IN (SELECT kind, scope_id FROM unnest(%s::text[], %s::text[]) AS t(kind, scope_id))
"""


# ---------------------------------------------------------------------------
# Normalizers.
#
# Module-level functions, not methods: the SQLite backend carries these as
# private methods, and several plane mixins need the same wording.  Defining
# them here as functions keeps two mixins in one MRO from silently shadowing
# each other's copy with a subtly different message.


def _normalize_tenant_id(tenant_id: str) -> str:
    normalized = tenant_id.strip()
    if not normalized:
        raise ValueError("tenant_id cannot be blank")
    return normalized


def _normalize_agent_id(agent_id: str) -> str:
    """Trim-and-reject only.

    Deliberately *not* :func:`memotron.identity.normalize_agent_id`: the
    SQLite backend applies the full identity grammar on registration and this
    weaker rule everywhere else, so a motive assignment or purge still addresses
    an agent whose id predates the grammar.
    """
    normalized = agent_id.strip()
    if not normalized:
        raise ValueError("agent_id cannot be blank")
    return normalized


def _normalize_non_blank(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be blank")
    return normalized


def _normalize_llm_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized not in _LLM_PROVIDERS:
        if normalized == "anthropic":
            raise ValueError(
                "provider 'anthropic' is retired: Memotron reaches Claude "
                "through the JedAI Gateway. Use provider 'litellm' with the "
                "gateway base_url and an undated model alias."
            )
        raise ValueError("provider must be 'litellm' or 'openai'")
    return normalized


def _now_text() -> str:
    """Now, in the exact ISO-8601 text the SQLite backend wrote."""
    return str(datetime_to_text(datetime.now(UTC)))


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated, blank-free — the SQLite delete helpers' input rule."""
    return tuple(sorted({str(value) for value in values if str(value).strip()}))


class _RacedRegistrationError(Exception):
    """Internal marker: a registration lost a race and its transaction is gone.

    Private to this module and never allowed to escape
    :meth:`GovernancePlaneMixin.register_tenant_agent`, which converts it into
    either the refreshed registration or the ``ValueError`` the interface
    specifies.  It exists only to carry control out of an aborted transaction,
    since no further statement can run inside one.
    """


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.postgres._protocol import ComposedPostgresBackend

    _Base = ComposedPostgresBackend
else:
    _Base = object


class GovernancePlaneMixin(_Base):
    """Erasure keys, attestations, tenant identity/config, and the tenant purge."""

    # Provided by the composing backend.
    _engine: Any
    _memory_graph: Any
    key_manager: Any

    # ------------------------------------------------------------------ erasure keys (DW-007)

    def get_or_create_governance_key(self, scope_key: str, subject_key: str = "") -> bytes:
        """Return (or elect) the per-scope 256-bit content DEK.

        **The single most important correctness property in this file.**  The DEK
        is random and is persisted only in KEK-wrapped form, so if two replicas
        each generate one and only one row survives, every field sealed under the
        losing DEK is permanently unreadable — a silent, unrecoverable data loss
        that reports success at write time.  The insert is therefore the election:
        ``ON CONFLICT (scope_key, subject_key) DO NOTHING RETURNING`` lets the
        primary key arbitrate, and the losing branch re-reads the winner's row and
        uses *that* key.  The DEK generated on the losing branch is discarded
        without ever being returned or used to seal anything.

        A crypto-shredded scope no longer accepts crypto-governed writes:
        requesting its key is a hard error on both the found and the raced branch.
        """
        with self._engine.transaction():
            existing = self._engine.fetchone(
                """
                SELECT wrapped_dek FROM governance_keys
                WHERE scope_key = %s AND subject_key = %s
                """,
                (scope_key, subject_key),
            )
            if existing is not None:
                return self._require_live_key(scope_key, str(existing["wrapped_dek"]))

            dek = secrets.token_bytes(CONTENT_KEY_BYTES)
            elected = self._engine.fetchone(
                """
                INSERT INTO governance_keys (
                    scope_key, subject_key, wrapped_dek, kek_id, key_algorithm,
                    created_at, shredded_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, '')
                ON CONFLICT (scope_key, subject_key) DO NOTHING
                RETURNING wrapped_dek
                """,
                (
                    scope_key,
                    subject_key,
                    self.key_manager.wrap_dek(dek),
                    self.key_manager.key_id(),
                    _KEY_ALGORITHM,
                    _now_text(),
                ),
            )
            if elected is not None:
                # Our wrapped DEK is the persisted one, so the plaintext DEK we
                # hold is the scope's key.  (SQLite returns the same object here;
                # unwrapping the row back would only re-derive it.)
                return dek

            # Lost the election.  ``DO NOTHING`` waited for the conflicting
            # transaction to finish, so under READ COMMITTED the next statement's
            # snapshot sees the winner's committed row.  The DEK generated above
            # is dropped on the floor: it sealed nothing and must never be
            # returned, or this caller would write content nobody can open.
            winner = self._engine.fetchone(
                """
                SELECT wrapped_dek FROM governance_keys
                WHERE scope_key = %s AND subject_key = %s
                """,
                (scope_key, subject_key),
            )
            if winner is None:  # pragma: no cover - the conflicting row cannot vanish
                raise ContentKeyUnavailableError(
                    f"governance key for scope {scope_key!r} conflicted but could not be read back"
                )
            return self._require_live_key(scope_key, str(winner["wrapped_dek"]))

    def _require_live_key(self, scope_key: str, wrapped: str) -> bytes:
        """Unwrap a stored DEK, or refuse because the scope was erased.

        An empty ``wrapped_dek`` *is* the tombstone: the row survives so the
        erasure certificate can still be produced, but the key material is gone.
        """
        if wrapped:
            return self.key_manager.unwrap_dek(wrapped)
        raise ContentKeyUnavailableError(
            f"governance key for scope {scope_key!r} was crypto-shredded; the scope's "
            "content is unrecoverable and the scope no longer accepts crypto-governed writes"
        )

    def shred_governance_key(self, scope_key: str, subject_key: str = "") -> bool:
        """Destroy the wrapped DEK so the scope's sealed content becomes unrecoverable.

        DW-007 fail-closed: the row is taken ``FOR UPDATE`` first, the key's
        existence is confirmed *under that lock*, and only then is ``wrapped_dek``
        zeroed and ``shredded_at`` stamped — one transaction, no window in which
        the scope is both erased and still serving key material.  A concurrent
        :meth:`get_or_create_governance_key` either completed before the lock was
        taken or blocks and then sees the tombstone and refuses.

        Returns True if a key existed, False otherwise.  Re-shredding an already
        shredded key is *not* an error: it returns True and refreshes the stamp,
        matching the SQLite backend exactly (whose update was unconditional).

        Production: additionally call KMS.ScheduleKeyDeletion on a per-scope CMK.
        """
        with self._engine.transaction():
            row = self._engine.fetchone(
                """
                SELECT wrapped_dek FROM governance_keys
                WHERE scope_key = %s AND subject_key = %s
                FOR UPDATE
                """,
                (scope_key, subject_key),
            )
            if row is None:
                return False
            self._engine.execute(
                """
                UPDATE governance_keys
                SET wrapped_dek = '', shredded_at = %s
                WHERE scope_key = %s AND subject_key = %s
                """,
                (_now_text(), scope_key, subject_key),
            )
            return True

    def get_governance_key(self, scope_key: str, subject_key: str = "") -> bytes | None:
        """Return the unwrapped DEK, or None if the key was shredded or never created."""
        row = self._engine.fetchone(
            """
            SELECT wrapped_dek FROM governance_keys
            WHERE scope_key = %s AND subject_key = %s
            """,
            (scope_key, subject_key),
        )
        if row is None:
            return None
        wrapped = str(row["wrapped_dek"])
        if not wrapped:
            return None  # Shredded
        return self.key_manager.unwrap_dek(wrapped)

    def governance_key_state(self, scope_key: str, subject_key: str = "") -> dict[str, Any] | None:
        """Key-lifecycle facts for the erasure certificate; never key material.

        ``scope_key`` / ``subject_key`` are echoed from the arguments rather than
        the row, as in SQLite, so the certificate names the scope that was asked
        about.
        """
        row = self._engine.fetchone(
            """
            SELECT wrapped_dek, kek_id, key_algorithm, created_at, shredded_at
            FROM governance_keys WHERE scope_key = %s AND subject_key = %s
            """,
            (scope_key, subject_key),
        )
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

    def reveal(self, scope_key: str, value: Any) -> Any:
        """WS-12 decrypt-on-read: resolve a stored content field for a read surface.

        Plaintext passes through; sealed content opens under the scope's live DEK
        and resolves to the shredded placeholder after crypto-shred.  The crypto
        is :mod:`memotron.crypto`, unchanged — this plane only supplies the key.
        """
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
        if not is_sealed_content(value):
            return None
        revealed = reveal_json(value, self.get_governance_key(scope_key))
        return revealed if isinstance(revealed, list) and revealed else None

    # ------------------------------------------------------------------ attestations

    def _formation_signer_rows(self, signer_id: str) -> tuple[str | None, tuple[tuple[str, str], ...]]:
        """Two SELECTs for the signer diagnostic. No decision here -- see `_shared/_governance`.

        Deliberately NOT wrapped in `self._engine.transaction()`: this is a diagnostic read and
        must stay callable while another transaction is in flight.
        """
        row = self._engine.fetchone(
            """
            SELECT wrapped_private_key FROM formation_contract_signing_keys
            WHERE signer_id = %s
            """,
            (signer_id,),
        )
        others = self._engine.fetchall(
            """
            SELECT signer_id, created_at FROM formation_contract_signing_keys
            WHERE signer_id <> %s ORDER BY created_at
            """,
            (signer_id,),
        )
        wrapped = None if row is None else str(row["wrapped_private_key"])
        return wrapped, tuple((str(r["signer_id"]), str(r["created_at"])) for r in others)

    def attest_formation_contract(
        self, *, contract_digest: str, certification_verdict: str = "uncertified"
    ) -> FormationContractAttestation:
        """Sign a DSSE/in-toto verdict using a store-local KMS-wrapped key.

        The private Ed25519 material is never stored in plaintext; the configured
        ``KeyManager`` owns the wrapping key, so a production store can substitute
        a KMS-backed manager without changing the attestation format.

        The provisioning race matters for the same reason the DEK's does: two
        replicas generating a signing key concurrently would leave one of them
        signing with a private key that never reached the table, and the resulting
        attestation would verify against a public key nobody can produce again.
        The ``ON CONFLICT (signer_id) DO NOTHING RETURNING`` election, and the
        re-read of the winner's wrapped key on the losing branch, prevent that.
        """
        signer_id = formation_signer_id(self.key_manager)
        with self._engine.transaction():
            row = self._engine.fetchone(
                """
                SELECT wrapped_private_key FROM formation_contract_signing_keys
                WHERE signer_id = %s
                """,
                (signer_id,),
            )
            if row is None:
                candidate = Ed25519PrivateKey.generate().private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                elected = self._engine.fetchone(
                    """
                    INSERT INTO formation_contract_signing_keys (
                        signer_id, wrapped_private_key, created_at
                    )
                    VALUES (%s, %s, %s)
                    ON CONFLICT (signer_id) DO NOTHING
                    RETURNING wrapped_private_key
                    """,
                    (signer_id, self.key_manager.wrap_dek(candidate), _now_text()),
                )
                private_key = candidate if elected is not None else self._stored_signing_key(signer_id)
            else:
                private_key = self.key_manager.unwrap_dek(str(row["wrapped_private_key"]))

        from memotron.attestations import attest_formation_contract

        return attest_formation_contract(
            contract_digest=contract_digest,
            certification_verdict=certification_verdict,
            private_key_bytes=private_key,
            key_id=signer_id,
        )

    def _stored_signing_key(self, signer_id: str) -> bytes:
        """Read back the signing key that won the provisioning election."""
        row = self._engine.fetchone(
            """
            SELECT wrapped_private_key FROM formation_contract_signing_keys
            WHERE signer_id = %s
            """,
            (signer_id,),
        )
        if row is None:  # pragma: no cover - the conflicting row cannot vanish
            raise ValueError(f"formation contract signing key {signer_id!r} conflicted but could not be read back")
        return self.key_manager.unwrap_dek(str(row["wrapped_private_key"]))

    # ------------------------------------------------------------------ tenant identity (DW-018)

    # -- the per-key registry (DW-026 / DW-030) -----------------------------------

    @staticmethod
    def _normalize_key_alias(key_alias: str) -> str:
        """Canonical alias. Stripped, and blank refused.

        MUST match the SQLite rule exactly (`sqlite/_governance.py`). #158 F2 was this
        divergence one layer up: the client stripped differently from storage, and a
        whitespace variant pinned a revoked credential indefinitely. The parity test
        `test_alias_whitespace_is_stripped_on_both_write_and_read` runs on both engines.
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
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_principal = principal_id.strip()
        if not normalized_principal:
            raise ValueError("principal_id cannot be blank")
        now = datetime.now(UTC).isoformat()

        existing = self._engine.fetchone(
            "SELECT tenant_id, created_at FROM key_principals WHERE key_alias = %s",
            (alias,),
        )
        if existing is not None and str(existing["tenant_id"]) != normalized_tenant:
            # The DW-030 guarantee, enforced by the schema (key_alias is the PRIMARY KEY)
            # and refused here rather than overwritten -- overwriting would silently move
            # a live caller to another tenant.
            raise ValueError(f"key_alias {alias!r} is already bound to tenant {str(existing['tenant_id'])!r}")

        created = str(existing["created_at"]) if existing is not None else now
        self._engine.execute(
            """
            INSERT INTO key_principals (
                key_alias, principal_id, tenant_id, agent_id, role,
                default_scope_key, allowed_scope_keys_json, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (key_alias) DO UPDATE SET
                principal_id = EXCLUDED.principal_id,
                agent_id = EXCLUDED.agent_id,
                role = EXCLUDED.role,
                default_scope_key = EXCLUDED.default_scope_key,
                allowed_scope_keys_json = EXCLUDED.allowed_scope_keys_json,
                updated_at = EXCLUDED.updated_at
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
        resolved = self.principal_for_key_alias(alias)
        assert resolved is not None
        return resolved

    def principal_for_key_alias(self, key_alias: str) -> dict[str, Any] | None:
        alias = self._normalize_key_alias(key_alias)
        row = self._engine.fetchone(
            """
            SELECT key_alias, principal_id, tenant_id, agent_id, role,
                   default_scope_key, allowed_scope_keys_json
            FROM key_principals
            WHERE key_alias = %s
            """,
            (alias,),
        )
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
        return bool(self._engine.execute("DELETE FROM key_principals WHERE key_alias = %s", (alias,)))

    def register_tenant_agent(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        name: str,
        source: str = "runtime",
    ) -> dict[str, Any]:
        """Register (or refresh) a tenant-local agent identity.

        Both the agent id (case-insensitively) and the agent name must be unique
        inside the tenant.  Refresh advances ``last_seen_at`` only: ``created_at``,
        the stored ``agent_id`` spelling, the name, and the source all belong to
        the first registration and are preserved, so a later call with different
        casing does not rewrite the registry.

        Two locks do the work the SQLite schema's unique indexes used to do.  The
        migrated schema keeps ``tenant_agents_id_key_idx`` and
        ``tenant_agents_name_key_idx`` but *non-unique* (see the module docstring),
        so the database will not reject a second row for the same casefolded id or
        name.  The refresh path needs nothing extra — it holds the row through
        ``FOR UPDATE`` and the upsert.  The create path takes a transaction-scoped
        advisory lock on the tenant and then re-runs both uniqueness probes under
        it, so two replicas registering colliding identities serialise and the
        loser sees the winner's row.  ``ON CONFLICT (tenant_id, agent_id) DO
        UPDATE`` is guarded by ``name_key`` so that even a raced insert refuses to
        rebind an id to a different name; ``UniqueViolation`` is still caught, both
        as a backstop and so this stays correct the day a migration makes those two
        indexes unique.

        A caught ``UniqueViolation`` has already aborted its transaction, so the
        recovery SQLite performed inline (roll back, re-read, retry when the
        winner turned out to carry the same identity) has to happen *after* this
        transaction unwinds — anything else would hit ``current transaction is
        aborted``.  Hence the one-attempt helper below.
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_agent = normalize_agent_id(agent_id)
        normalized_name = normalize_agent_name(name)
        normalized_agent_key = agent_id_key(normalized_agent)
        normalized_name_key = agent_name_key(normalized_name)
        normalized_source = source.strip() or "runtime"

        try:
            return self._register_tenant_agent_once(
                normalized_tenant=normalized_tenant,
                normalized_agent=normalized_agent,
                normalized_agent_key=normalized_agent_key,
                normalized_name=normalized_name,
                normalized_name_key=normalized_name_key,
                normalized_source=normalized_source,
            )
        except _RacedRegistrationError as raced:
            registered = self.tenant_agent(tenant_id=normalized_tenant, agent_id=normalized_agent)
            if registered is not None and agent_name_key(str(registered["agent_name"])) == normalized_name_key:
                # The winner registered the identical identity, so this call was a
                # refresh after all: retry, which now takes the existing-row path.
                return self.register_tenant_agent(
                    tenant_id=normalized_tenant,
                    agent_id=normalized_agent,
                    name=normalized_name,
                    source=normalized_source,
                )
            raise ValueError(
                f"agent identity collision in tenant {normalized_tenant!r}; agent_id and agent_name must each be unique"
            ) from raced.__cause__

    def _register_tenant_agent_once(
        self,
        *,
        normalized_tenant: str,
        normalized_agent: str,
        normalized_agent_key: str,
        normalized_name: str,
        normalized_name_key: str,
        normalized_source: str,
    ) -> dict[str, Any]:
        """One registration attempt inside one transaction.

        Takes already-normalized values, so the identity grammar runs exactly once
        per public call.  Raises :class:`_RacedRegistrationError` when the outcome can
        only be decided after this transaction has rolled back; see
        :meth:`register_tenant_agent`.
        """
        from psycopg import errors as psycopg_errors

        now = _now_text()

        with self._engine.transaction():
            existing = self._lock_registered_agent(normalized_tenant, normalized_agent_key)
            if existing is None:
                # Create path: the identity does not exist yet, so there is no row
                # to lock.  Serialise on the tenant and re-probe under the lock.
                self._lock_tenant(normalized_tenant)
                existing = self._lock_registered_agent(normalized_tenant, normalized_agent_key)

            if existing is not None:
                if str(existing["name_key"]) != normalized_name_key:
                    raise ValueError(
                        f"agent_id {normalized_agent!r} is already registered to "
                        f"agent_name {str(existing['name'])!r} in tenant {normalized_tenant!r}"
                    )
                self._engine.execute(
                    """
                    UPDATE tenant_agents SET last_seen_at = %s
                    WHERE tenant_id = %s AND agent_id_key = %s
                    """,
                    (now, normalized_tenant, normalized_agent_key),
                )
                return {
                    "tenant_id": normalized_tenant,
                    "agent_id": str(existing["agent_id"]),
                    "agent_name": str(existing["name"]),
                    "source": str(existing["source"]),
                    "created_at": str(existing["created_at"]),
                    "last_seen_at": now,
                    "created": False,
                }

            name_owner = self._engine.fetchone(
                """
                SELECT agent_id FROM tenant_agents
                WHERE tenant_id = %s AND name_key = %s
                """,
                (normalized_tenant, normalized_name_key),
            )
            if name_owner is not None:
                raise ValueError(
                    f"agent_name {normalized_name!r} is already registered to "
                    f"agent_id {str(name_owner['agent_id'])!r} in tenant {normalized_tenant!r}"
                )

            try:
                row = self._engine.fetchone(
                    """
                    INSERT INTO tenant_agents (
                        tenant_id, agent_id, agent_id_key, name, name_key, source,
                        created_at, last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, agent_id) DO UPDATE
                        SET last_seen_at = excluded.last_seen_at
                        WHERE tenant_agents.name_key = excluded.name_key
                    RETURNING tenant_id, agent_id, name, source, created_at, last_seen_at
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
            except psycopg_errors.UniqueViolation as exc:
                # Reachable once the identity-key indexes are unique: a row exists
                # under a different primary key (another spelling of the id, or
                # the name is taken).  Which of those it is can only be read after
                # the rollback, so hand the decision back to the caller.
                raise _RacedRegistrationError from exc

            if row is None:
                # The upsert's ``name_key`` guard refused: a row for this exact
                # agent_id exists under a different name.  Nothing to re-read —
                # that is the collision, with SQLite's wording.
                raise ValueError(
                    f"agent identity collision in tenant {normalized_tenant!r}; "
                    "agent_id and agent_name must each be unique"
                )

            return {
                "tenant_id": normalized_tenant,
                "agent_id": str(row["agent_id"]),
                "agent_name": str(row["name"]),
                "source": str(row["source"]),
                "created_at": str(row["created_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                # A raced insert returns the winner's row, whose ``created_at`` is
                # its own stamp, not ours — which is precisely the "not created by
                # this call" signal SQLite produced by re-reading and recursing.
                "created": str(row["created_at"]) == now,
            }

    def _lock_registered_agent(self, tenant_id: str, agent_id_key_value: str) -> dict[str, Any] | None:
        """Read one registration by its casefolded id, holding it for the write.

        Seeks ``tenant_agents_id_key_idx``.  ``FOR UPDATE`` is what makes
        "check the name, then stamp last_seen_at" indivisible.
        """
        return self._engine.fetchone(
            """
            SELECT tenant_id, agent_id, agent_id_key, name, name_key, source,
                   created_at, last_seen_at
            FROM tenant_agents
            WHERE tenant_id = %s AND agent_id_key = %s
            FOR UPDATE
            """,
            (tenant_id, agent_id_key_value),
        )

    def _lock_tenant(self, tenant_id: str) -> None:
        """Serialise the multi-row invariants of one tenant for this transaction.

        Row locks cannot cover an invariant over rows that do not exist yet — the
        first prompt version, the first config version, the first registration of
        an agent id — so the lock is keyed on the tenant identity instead of on a
        row.  Transaction-scoped, so the commit or rollback always releases it.  A
        hash collision between two tenants costs a little serialisation and
        nothing else.
        """
        self._engine.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
            (f"tenant_governance\x1f{tenant_id}", _TENANT_LOCK_SEED),
        )

    def tenant_agent(self, *, tenant_id: str, agent_id: str) -> dict[str, Any] | None:
        """One registered agent, or None.  Matched case-insensitively by id key."""
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_agent_key = agent_id_key(agent_id)
        row = self._engine.fetchone(
            """
            SELECT tenant_id, agent_id, name, source, created_at, last_seen_at
            FROM tenant_agents
            WHERE tenant_id = %s AND agent_id_key = %s
            """,
            (normalized_tenant, normalized_agent_key),
        )
        return None if row is None else tenant_agent_from_row(row)

    def tenant_agents(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        """All registered agents for a tenant, oldest first."""
        rows = self._engine.fetchall(
            """
            SELECT tenant_id, agent_id, name, source, created_at, last_seen_at
            FROM tenant_agents
            WHERE tenant_id = %s
            ORDER BY created_at COLLATE "C", agent_id COLLATE "C"
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        return tuple(tenant_agent_from_row(row) for row in rows)

    def known_tenant_ids(self) -> tuple[str, ...]:
        """Every tenant id this store holds persisted tenant state for.

        The union across every tenant-keyed table: a tenant that only registered
        an agent, or only sealed a credential, is just as real as one that saved
        a project-memory policy.
        """
        rows = self._engine.fetchall(
            """
            SELECT DISTINCT tenant_id FROM tenant_agents
            UNION SELECT DISTINCT tenant_id FROM tenant_llm_credentials
            UNION SELECT DISTINCT tenant_id FROM tenant_prompt_versions
            UNION SELECT DISTINCT tenant_id FROM tenant_prompt_overrides
            UNION SELECT DISTINCT tenant_id FROM project_memory_config_versions
            UNION SELECT DISTINCT tenant_id FROM agent_motive_assignments
            """
        )
        found = {
            str(row["tenant_id"]).strip()
            for row in rows
            if row["tenant_id"] is not None and str(row["tenant_id"]).strip()
        }
        return tuple(sorted(found))

    def deregister_tenant_agent(self, *, tenant_id: str, agent_id: str) -> bool:
        """Detach one agent from a tenant WITHOUT touching its agent-scope memory.

        An ownership change, not an erasure: rows in ``agent:<agent_id>`` are
        left alone.  The agent's ``agent_motive_assignments`` row is removed
        first (it foreign-keys onto ``tenant_agents``).  Returns False when the
        agent was not registered to this tenant.
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_agent_key = agent_id_key(normalize_agent_id(agent_id))
        with self._engine.transaction():
            row = self._engine.fetchone(
                "SELECT agent_id FROM tenant_agents WHERE tenant_id = %s AND agent_id_key = %s",
                (normalized_tenant, normalized_agent_key),
            )
            if row is None:
                return False
            self._engine.execute(
                "DELETE FROM agent_motive_assignments WHERE tenant_id = %s AND agent_id = %s",
                (normalized_tenant, str(row["agent_id"])),
            )
            self._engine.execute(
                "DELETE FROM tenant_agents WHERE tenant_id = %s AND agent_id_key = %s",
                (normalized_tenant, normalized_agent_key),
            )
        return True

    def set_agent_motive_assignment(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        motive_name: str,
        source: str,
    ) -> dict[str, str]:
        """Upsert the motive assigned to a registered agent.

        One statement: ``created_at`` is absent from the ``DO UPDATE`` set list, so
        it survives without the read-then-write SQLite needed, and ``RETURNING``
        yields the stored row rather than a second read.  The explicit
        registration probe keeps the interface's ``ValueError`` contract; the
        foreign key to ``tenant_agents`` is the backstop for a concurrent
        deregistration, and it is translated to the same message.
        """
        from psycopg import errors as psycopg_errors

        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_agent = _normalize_agent_id(agent_id)
        normalized_motive = _normalize_non_blank(motive_name, "motive_name")
        normalized_source = _normalize_non_blank(source, "source")
        now = _now_text()

        with self._engine.transaction():
            registered = self._engine.fetchvalue(
                """
                SELECT EXISTS (
                    SELECT 1 FROM tenant_agents WHERE tenant_id = %s AND agent_id = %s
                )
                """,
                (normalized_tenant, normalized_agent),
            )
            if not registered:
                raise ValueError(f"agent {normalized_agent!r} is not registered for tenant {normalized_tenant!r}")
            try:
                row = self._engine.fetchone(
                    """
                    INSERT INTO agent_motive_assignments (
                        tenant_id, agent_id, motive_name, source, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, agent_id) DO UPDATE SET
                        motive_name = excluded.motive_name,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    RETURNING tenant_id, agent_id, motive_name, source, created_at, updated_at
                    """,
                    (
                        normalized_tenant,
                        normalized_agent,
                        normalized_motive,
                        normalized_source,
                        now,
                        now,
                    ),
                )
            except psycopg_errors.ForeignKeyViolation as exc:
                raise ValueError(
                    f"agent {normalized_agent!r} is not registered for tenant {normalized_tenant!r}"
                ) from exc
            if row is None:  # pragma: no cover - an unconditional upsert returns its row
                raise ValueError(f"motive assignment for agent {normalized_agent!r} could not be stored")
            return self._motive_assignment_from_row(row)

    def agent_motive_assignment(self, *, tenant_id: str, agent_id: str) -> dict[str, str] | None:
        """The motive assignment for one agent, or None."""
        row = self._engine.fetchone(
            """
            SELECT tenant_id, agent_id, motive_name, source, created_at, updated_at
            FROM agent_motive_assignments
            WHERE tenant_id = %s AND agent_id = %s
            """,
            (_normalize_tenant_id(tenant_id), _normalize_agent_id(agent_id)),
        )
        return None if row is None else self._motive_assignment_from_row(row)

    def agent_motive_assignments(self, tenant_id: str) -> tuple[dict[str, str], ...]:
        """All motive assignments for a tenant, by agent id."""
        rows = self._engine.fetchall(
            """
            SELECT tenant_id, agent_id, motive_name, source, created_at, updated_at
            FROM agent_motive_assignments
            WHERE tenant_id = %s
            ORDER BY agent_id COLLATE "C"
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        return tuple(self._motive_assignment_from_row(row) for row in rows)

    @staticmethod
    def _motive_assignment_from_row(row: dict[str, Any]) -> dict[str, str]:
        return {
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

    # ------------------------------------------------------------------ tenant configuration

    def set_tenant_prompt_override(
        self,
        *,
        tenant_id: str,
        prompt_profile: str,
        prompt_profile_version: str,
        override: dict[str, Any],
    ) -> dict[str, Any]:
        """Upsert the tenant's prompt-profile override.

        ``created_at`` stays out of the ``DO UPDATE`` set list, so the first write
        owns it without the pre-read SQLite performed.
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_profile = _normalize_non_blank(prompt_profile, "prompt_profile")
        normalized_version = _normalize_non_blank(prompt_profile_version, "prompt_profile_version")
        override_json = json_dumps(override, "tenant prompt override")
        now = _now_text()
        row = self._engine.fetchone(
            """
            INSERT INTO tenant_prompt_overrides (
                tenant_id, prompt_profile, prompt_profile_version, override,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                prompt_profile = excluded.prompt_profile,
                prompt_profile_version = excluded.prompt_profile_version,
                override = excluded.override,
                updated_at = excluded.updated_at
            RETURNING tenant_id, prompt_profile, prompt_profile_version, override,
                      created_at, updated_at
            """,
            (
                normalized_tenant,
                normalized_profile,
                normalized_version,
                override_json,
                now,
                now,
            ),
        )
        # ``or {}`` mirrors SQLite's read-back-after-write, which could not see a
        # row deleted in between.
        return {} if row is None else self._prompt_override_from_row(row)

    def tenant_prompt_override(self, tenant_id: str) -> dict[str, Any] | None:
        """The tenant's prompt override, or None."""
        normalized_tenant = _normalize_tenant_id(tenant_id)
        row = self._engine.fetchone(
            """
            SELECT tenant_id, prompt_profile, prompt_profile_version, override,
                   created_at, updated_at
            FROM tenant_prompt_overrides WHERE tenant_id = %s
            """,
            (normalized_tenant,),
        )
        return None if row is None else self._prompt_override_from_row(row)

    def _prompt_override_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        override = as_json(row["override"])
        if not isinstance(override, dict):
            raise ValueError(f"tenant prompt override for {str(row['tenant_id'])!r} is invalid")
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
        """Append a new tenant prompt version and mark it active.

        "Exactly one active version per tenant" spans rows, so the whole save is
        one transaction under the tenant lock: allocate the next version, insert
        it, and re-establish the flag with a single ``UPDATE ... SET active =
        (version = %s)``.  SQLite's deactivate-then-activate pair would let two
        concurrent saves both end up active (and could race two inserts onto the
        same version number, which the primary key would reject).  Note the flip
        deliberately leaves ``updated_at`` alone on the demoted rows, as SQLite's
        blanket ``SET active = 0`` did.
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_text = _normalize_non_blank(prompt_text, "prompt_text")
        normalized_motive = motive_name.strip()
        normalized_profile = source_profile.strip()
        normalized_profile_version = source_profile_version.strip()
        now = _now_text()

        with self._engine.transaction():
            self._lock_tenant(normalized_tenant)
            version = self._next_version("tenant_prompt_versions", "tenant-v", normalized_tenant)
            self._engine.execute(
                f"""
                INSERT INTO tenant_prompt_versions ({_TENANT_PROMPT_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s)
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
            self._engine.execute(
                """
                UPDATE tenant_prompt_versions
                SET active = (version = %s)
                WHERE tenant_id = %s AND active IS DISTINCT FROM (version = %s)
                """,
                (version, normalized_tenant, version),
            )
            return self.active_tenant_prompt_version(normalized_tenant) or {}

    def active_tenant_prompt_version(self, tenant_id: str) -> dict[str, Any] | None:
        """The active tenant prompt version, or None.  Seeks tenant_prompt_versions_tenant_idx."""
        row = self._engine.fetchone(
            f"""
            SELECT {_TENANT_PROMPT_COLUMNS}
            FROM tenant_prompt_versions
            WHERE tenant_id = %s AND active
            ORDER BY created_at COLLATE "C" DESC, version COLLATE "C" DESC
            LIMIT 1
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        return tenant_prompt_version_from_row(row)

    def tenant_prompt_versions(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        """All tenant prompt versions, newest first."""
        rows = self._engine.fetchall(
            f"""
            SELECT {_TENANT_PROMPT_COLUMNS}
            FROM tenant_prompt_versions
            WHERE tenant_id = %s
            ORDER BY created_at COLLATE "C" DESC, version COLLATE "C" DESC
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        return tuple(version for row in rows if (version := tenant_prompt_version_from_row(row)) is not None)

    def save_project_memory_config(
        self, *, tenant_id: str, config: dict[str, Any], configured_by: str
    ) -> dict[str, Any]:
        """Append a new project memory-config version and mark it active.

        Same one-transaction, one-flip treatment of the "exactly one active"
        invariant as :meth:`save_tenant_prompt_version`.
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_configured_by = _normalize_non_blank(configured_by, "configured_by")
        config_json = json_dumps(config, "project memory config")
        now = _now_text()

        with self._engine.transaction():
            self._lock_tenant(normalized_tenant)
            version = self._next_version("project_memory_config_versions", "project-v", normalized_tenant)
            self._engine.execute(
                f"""
                INSERT INTO project_memory_config_versions ({_PROJECT_CONFIG_COLUMNS})
                VALUES (%s, %s, %s::jsonb, %s, true, %s, %s)
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
            self._engine.execute(
                """
                UPDATE project_memory_config_versions
                SET active = (version = %s)
                WHERE tenant_id = %s AND active IS DISTINCT FROM (version = %s)
                """,
                (version, normalized_tenant, version),
            )
            return self.active_project_memory_config(normalized_tenant) or {}

    def active_project_memory_config(self, tenant_id: str) -> dict[str, Any] | None:
        """The active project memory config, or None.

        Seeks ``project_memory_config_versions_tenant_idx``.  SQLite broke ties on
        ``rowid``; :data:`_PROJECT_VERSION_ORDINAL` is the portable stand-in.
        """
        row = self._engine.fetchone(
            f"""
            SELECT {_PROJECT_CONFIG_COLUMNS}
            FROM project_memory_config_versions
            WHERE tenant_id = %s AND active
            ORDER BY created_at COLLATE "C" DESC, {_PROJECT_VERSION_ORDINAL} DESC
            LIMIT 1
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        return self._project_memory_config_from_row(row)

    def project_memory_config_versions(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        """All project memory-config versions, newest first."""
        rows = self._engine.fetchall(
            f"""
            SELECT {_PROJECT_CONFIG_COLUMNS}
            FROM project_memory_config_versions
            WHERE tenant_id = %s
            ORDER BY created_at COLLATE "C" DESC, {_PROJECT_VERSION_ORDINAL} DESC
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        return tuple(config for row in rows if (config := self._project_memory_config_from_row(row)) is not None)

    def _project_memory_config_from_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = as_json(row["config"])
        if not isinstance(payload, dict):
            raise ValueError("stored project memory config must be a JSON object")
        # The stored document is spread first so the identity/lifecycle fields
        # always win, exactly as SQLite composed it.
        return {
            **payload,
            "tenant_id": str(row["tenant_id"]),
            "version": str(row["version"]),
            "configured_by": str(row["configured_by"]),
            "active": bool(row["active"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _next_version(self, table: str, prefix: str, tenant_id: str) -> str:
        """Allocate ``<prefix><n+1>`` from the tenant's highest generated version.

        The scan-and-max SQLite did in Python is one indexed aggregate here.  Must
        be called with the tenant lock held: the value is only unique because
        nothing else may insert for this tenant until the transaction commits.
        ``table`` and ``prefix`` are module constants, never caller input.
        """
        highest = self._engine.fetchvalue(
            f"""
            SELECT COALESCE(MAX(
                CASE WHEN version ~ %s
                     THEN substr(version, %s)::bigint
                END
            ), 0)
            FROM {table} WHERE tenant_id = %s
            """,
            (f"^{prefix}[0-9]+$", len(prefix) + 1, tenant_id),
        )
        return f"{prefix}{int(highest or 0) + 1}"

    # ------------------------------------------------------------------ tenant LLM credentials (DW-006)

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
        """Seal and upsert a tenant's LLM credentials.

        The key provisioning and the sealed write share one transaction on
        purpose: a credential row that commits while its DEK does not would be
        unreadable forever, and a DEK that commits without the credential leaves an
        orphan key on the erasure certificate.

        The three ``embedding_*`` arguments are tri-state -- ``None`` PRESERVES what the
        tenant has, a value sets it, ``""`` clears it -- and are resolved by the SHARED
        rule, not a second copy of it. They landed on SQLite in WS-17 T18 and never here,
        so this method took five keyword arguments while SQLite took eight and
        `_migrate_llm_credentials` passed all eight unconditionally: migrating a tenant
        into any Postgres store raised TypeError (#163).
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_provider = _normalize_llm_provider(provider)
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("api_key cannot be blank")
        now = _now_text()

        with self._engine.transaction():
            credential_key = self.get_or_create_governance_key(
                f"tenant:{normalized_tenant}", subject_key=_LLM_CREDENTIAL_SUBJECT
            )
            sealed_key = seal_content(normalized_key, credential_key)
            # Read inside the same transaction as the upsert: the tri-state PRESERVE is a
            # read-then-write, and a bare SELECT followed by an UPDATE is a lost update
            # under two replicas.
            current = self._engine.fetchone(
                """
                SELECT embedding_provider, embedding_base_url, embedding_model
                FROM tenant_llm_credentials WHERE tenant_id = %s
                FOR UPDATE
                """,
                (normalized_tenant,),
            )
            resolved_provider, resolved_base_url, resolved_model = resolve_embedding_endpoint(
                embedding_provider=embedding_provider,
                embedding_base_url=embedding_base_url,
                embedding_model=embedding_model,
                existing=(
                    (
                        str(current["embedding_provider"]),
                        str(current["embedding_base_url"]),
                        str(current["embedding_model"]),
                    )
                    if current is not None
                    else None
                ),
            )
            row = self._engine.fetchone(
                """
                INSERT INTO tenant_llm_credentials (
                    tenant_id, provider, api_key_sealed, base_url, model,
                    embedding_provider, embedding_base_url, embedding_model,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    provider = excluded.provider,
                    api_key_sealed = excluded.api_key_sealed,
                    base_url = excluded.base_url,
                    model = excluded.model,
                    embedding_provider = excluded.embedding_provider,
                    embedding_base_url = excluded.embedding_base_url,
                    embedding_model = excluded.embedding_model,
                    updated_at = excluded.updated_at
                RETURNING tenant_id, provider, base_url, model, created_at, updated_at
                """,
                (
                    normalized_tenant,
                    normalized_provider,
                    sealed_key,
                    base_url.strip(),
                    model.strip(),
                    resolved_provider,
                    resolved_base_url,
                    resolved_model,
                    now,
                    now,
                ),
            )
            # Sealed moments ago with the key this process holds, so readable by
            # construction -- no point paying for an unwrap to learn what we just did.
            return {} if row is None else self._credential_state_from_row(row, readable=True)

    def tenant_llm_credentials(self, tenant_id: str) -> dict[str, Any] | None:
        """The unsealed credentials, or None; raises once the scope key is shredded."""
        normalized_tenant = _normalize_tenant_id(tenant_id)
        row = self._engine.fetchone(
            "SELECT * FROM tenant_llm_credentials WHERE tenant_id = %s",
            (normalized_tenant,),
        )
        if row is None:
            return None
        key = self.get_governance_key(f"tenant:{normalized_tenant}", subject_key=_LLM_CREDENTIAL_SUBJECT)
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
            # The READ diverged too, and separately from the write (#163). Fixing only the
            # write left Postgres accepting the embedding endpoint and never returning it,
            # so `transports_for_tenant` would build an extraction transport from the
            # tenant's credential and silently fall back to the LOCAL embedder -- a
            # different vector space, with nothing raised. Caught by asserting the round
            # trip rather than the call.
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
        state = self.governance_key_state(f"tenant:{normalized_tenant}", subject_key=_LLM_CREDENTIAL_SUBJECT)
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
            key = self.get_governance_key(f"tenant:{normalized_tenant}", subject_key=_LLM_CREDENTIAL_SUBJECT)
        except (ValueError, ContentKeyUnavailableError):
            # ONLY key failures. An earlier version caught bare `Exception`, which meant a
            # database fault was reported as "credential unreadable" -- the same dishonest
            # status this whole change exists to remove, reintroduced inside the fix for it.
            # A storage error is a fault and must propagate as one.
            return False
        return llm_credential_is_readable(sealed, key)

    def tenant_llm_credential_state(self, tenant_id: str) -> dict[str, Any] | None:
        """Credential metadata without the key material, or None."""
        row = self._engine.fetchone(
            """
            SELECT tenant_id, provider, base_url, model, api_key_sealed, created_at, updated_at
            FROM tenant_llm_credentials WHERE tenant_id = %s
            """,
            (_normalize_tenant_id(tenant_id),),
        )
        if row is None:
            return None
        normalized = _normalize_tenant_id(tenant_id)
        readable = self._llm_credential_is_readable(normalized, str(row["api_key_sealed"]))
        shredded = False if readable else self._llm_credential_is_shredded(normalized)
        return self._credential_state_from_row(row, readable=readable, shredded=shredded)

    @staticmethod
    def _credential_state_from_row(row: dict[str, Any], *, readable: bool, shredded: bool = False) -> dict[str, Any]:
        """`readable` is passed in, never assumed. (#123)

        This used to hardcode ``has_api_key: True`` with the note "a stored row always
        carries a sealed key; the column is NOT NULL". That is true about PRESENCE and says
        nothing about whether the value can still be DECRYPTED -- and after a restart under
        an ephemeral KEK it could not be, while this kept reporting the tenant configured.
        Mirrors the SQLite change; see `_shared/_governance.llm_credential_is_readable`.
        """
        return {
            "tenant_id": str(row["tenant_id"]),
            "provider": str(row["provider"]),
            "has_api_key": readable,
            # UNREADABLE excludes a deliberate shred: page on the first, not the second.
            "api_key_unreadable": not readable and not shredded,
            "api_key_shredded": shredded,
            "base_url": str(row["base_url"]),
            "model": str(row["model"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def clear_tenant_llm_credentials(self, tenant_id: str) -> bool:
        """Delete stored credentials; True if any existed.

        The scope DEK is deliberately left alone: erasing key material is
        :meth:`shred_governance_key`'s job (and is irreversible for every other
        field sealed under the same scope).
        """
        removed = self._engine.execute(
            "DELETE FROM tenant_llm_credentials WHERE tenant_id = %s",
            (_normalize_tenant_id(tenant_id),),
        )
        return removed > 0

    # ------------------------------------------------------------------ cross-store purge

    def purge_tenant_state(
        self,
        tenant_id: str,
        *,
        agent_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Reset generated state for a hosted tenant without deleting raw episodes.

        Two preservation rules survive the port unchanged, because they are the
        whole point of the operation:

        * **Raw episodes are never deleted.**  Only their processing markers are
          reset, so the tenant's history can be re-derived.  ``raw_episodes_preserved``
          reports how many were kept.
        * **Tenant LLM credentials are never deleted.**  ``credentials_preserved``
          records whether any existed, and it is evaluated *before* the deletes.

        Everything operational commits in one transaction, so a failure part-way
        cannot leave (say) use events deleted but their prune ghosts intact.  The
        graph plane is reached through ``self._memory_graph`` and its idempotent
        primitives, which in a split deployment is a different store and therefore
        a different transaction — the operation is safe to re-run after a partial
        failure, per the cross-store rule.

        Child rows go before their parents (outcome events → use events → prune
        ghosts → relationships → nodes; motive assignments → agents) because those
        foreign keys are real here, not advisory as in SQLite.
        """
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_agents = {
            _normalize_agent_id(agent_id) for agent_id in agent_ids if agent_id is not None and str(agent_id).strip()
        }

        with self._engine.transaction():
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
            ordered_scope_keys = _ordered_unique(scope_keys)
            scope_pairs = [key.split(":", 1) for key in ordered_scope_keys if ":" in key]
            scope_kinds = [pair[0] for pair in scope_pairs]
            scope_ids = [pair[1] for pair in scope_pairs]

            # Episodes are read only to find their processing markers; the rows
            # themselves are preserved.  Predicate pushed into SQL rather than
            # validating every stored episode to compare one scope key.
            episode_uuids = {
                str(row["uuid"])
                for row in self._engine.fetchall(
                    "SELECT uuid FROM episodes WHERE " + _SCOPE_PAIR_PREDICATE.format(member="scope"),
                    (scope_kinds, scope_ids),
                )
            }

            # Graph-plane reads route through the Memory Graph surface so a split
            # deployment purges the right store.  ``nodes_for_scope`` is the
            # indexed interface read; relationships stay a full read because the
            # rule also matches edges whose *endpoints* belong to the tenant,
            # regardless of their own scope key, and no interface query expresses
            # that.
            node_uuids: set[str] = set()
            for scope_key in ordered_scope_keys:
                node_uuids.update(node.uuid for node in self._memory_graph.nodes_for_scope(scope_key))
            scope_key_set = set(ordered_scope_keys)
            relationship_uuids: set[str] = {
                relationship.uuid
                for relationship in self._memory_graph.relationships()
                if relationship.properties.get("scope_key") in scope_key_set
                or relationship.source_uuid in node_uuids
                or relationship.target_uuid in node_uuids
            }

            decision_uuids = {
                str(row["uuid"])
                for row in self._engine.fetchall(
                    f"""
                    SELECT uuid FROM dream_decisions
                    WHERE {_SCOPE_PAIR_PREDICATE.format(member="agent_scope")}
                       OR {_SCOPE_PAIR_PREDICATE.format(member="scope")}
                       OR agent_id = ANY(%s)
                    """,
                    (
                        scope_kinds,
                        scope_ids,
                        scope_kinds,
                        scope_ids,
                        list(ordered_agents),
                    ),
                )
            }

            counts: dict[str, Any] = {
                "tenant_id": normalized_tenant,
                "agent_ids": list(ordered_agents),
                "scope_keys": list(scope_keys),
                "raw_episodes_preserved": len(episode_uuids),
                "credentials_preserved": (self.tenant_llm_credential_state(normalized_tenant) is not None),
            }
            counts["processed_episodes_reset"] = self._delete_by_ids(
                "processed_episodes", "episode_uuid", episode_uuids
            )
            counts["episode_processing_reset"] = self._delete_by_ids(
                "episode_processing", "episode_uuid", episode_uuids
            )
            # Utility observations are derived runtime state and carry foreign
            # keys into the relationship graph. Delete children before parents.
            counts["memory_outcome_events_deleted"] = self._delete_by_scope_keys(
                "memory_outcome_events", ordered_scope_keys
            )
            counts["memory_use_events_deleted"] = self._delete_by_scope_keys("memory_use_events", ordered_scope_keys)
            counts["memory_prune_ghosts_deleted"] = self._delete_by_scope_keys(
                "memory_prune_ghosts", ordered_scope_keys
            )
            counts["quarantined_candidates_deleted"] = self._delete_by_scope_keys(
                "quarantined_candidates", ordered_scope_keys
            )
            counts["relationships_deleted"] = self._memory_graph.delete_relationships(relationship_uuids)
            counts["nodes_deleted"] = self._memory_graph.delete_nodes(node_uuids)
            counts["dream_decisions_deleted"] = self._delete_by_ids("dream_decisions", "uuid", decision_uuids)
            # Owned by the receipt plane (migration 4), scoped the same way, and
            # deleted here rather than through the ledger because the ledger
            # exposes no erasure primitive — exactly as in SQLite.
            counts["memory_receipts_deleted"] = self._delete_by_scope_keys("memory_receipts", ordered_scope_keys)
            counts["run_checkpoints_deleted"] = self._delete_by_scope_keys("run_checkpoints", ordered_scope_keys)
            # Maintenance bookkeeping is global, not per tenant: SQLite cleared it
            # wholesale so the next run re-derives from a clean slate.
            counts["dream_job_runs_deleted"] = self._engine.execute("DELETE FROM dream_job_runs")
            counts["job_state_deleted"] = self._engine.execute("DELETE FROM job_state")
            counts["agent_motive_assignments_deleted"] = self._engine.execute(
                "DELETE FROM agent_motive_assignments WHERE tenant_id = %s",
                (normalized_tenant,),
            )
            counts["tenant_agents_deleted"] = self._engine.execute(
                "DELETE FROM tenant_agents WHERE tenant_id = %s", (normalized_tenant,)
            )
            counts["tenant_prompt_overrides_deleted"] = self._engine.execute(
                "DELETE FROM tenant_prompt_overrides WHERE tenant_id = %s",
                (normalized_tenant,),
            )
            counts["tenant_prompt_versions_deleted"] = self._engine.execute(
                "DELETE FROM tenant_prompt_versions WHERE tenant_id = %s",
                (normalized_tenant,),
            )
            counts["project_memory_config_versions_deleted"] = self._engine.execute(
                "DELETE FROM project_memory_config_versions WHERE tenant_id = %s",
                (normalized_tenant,),
            )
            return counts

    # ------------------------------------------------------------------ delete helpers

    def _delete_by_ids(self, table: str, column: str, values: Iterable[str]) -> int:
        """Delete by identifier set; 0 for an empty set, as in SQLite.

        ``table`` and ``column`` are module-internal literals, never caller input;
        the values are always bound.
        """
        ordered = _ordered_unique(values)
        if not ordered:
            return 0
        return self._engine.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s)", (list(ordered),))

    def _delete_by_scope_keys(self, table: str, scope_keys: Sequence[str]) -> int:
        ordered = _ordered_unique(scope_keys)
        if not ordered:
            return 0
        return self._engine.execute(f"DELETE FROM {table} WHERE scope_key = ANY(%s)", (list(ordered),))


__all__ = ["GovernancePlaneMixin"]
