"""Provisioning content keys, shredding them, and proving the shred happened.

Six members carrying the strongest claim the product makes. `crypto_shred` destroys a
scope DEK so sealed content becomes unrecoverable; `erasure_certificate` and
`verify_erasure` are what turn that from an assertion into evidence. If these are
wrong the failure is not visible in any read path -- the data simply remains
recoverable while the certificate says otherwise."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    GovernancePolicy,
)
from memotron.crypto import content_commitment, is_sealed_content, seal_content
from memotron.erasure import (
    ErasureCertificate,
    issue_erasure_certificate,
    verify_erasure_certificate,
)
from memotron.models import (
    MemoryScope,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
)
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class CryptoGovernanceMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    def provision_governance_key(
        self,
        *,
        scope: MemoryScope,
        subject_key: str = "",
    ) -> bytes:
        """Provision (or retrieve) the per-scope content DEK for crypto-shred governance.

        Returns the 32-byte data-encryption key.  The DEK is persisted only in
        KEK-wrapped form (envelope encryption via the graph's KeyManager); write
        paths under CRYPTO_SHRED governance auto-provision it, so an explicit
        call is only needed when the caller wants the key material itself.
        """
        self._require_authorized_scope(scope)
        return self.graph.get_or_create_governance_key(scope.key, subject_key)

    async def crypto_shred(
        self,
        *,
        scope: MemoryScope,
        subject_key: str = "",
    ) -> dict[str, Any]:
        """Crypto-shred a scope: destroy the wrapped DEK so all sealed content is unrecoverable.

        WS-12: the scope's content plane (facts, object text, source snippets,
        entity names, embeddings, raw episode bodies) was stored ONLY as sealed
        ciphertext / keyed commitments under this DEK, so destroying the wrapped
        key IS the erasure.  Pending (unprocessed) episodes in the scope are
        retired so the erased evidence is never fed to an extractor again.

        What survives: relationship rows, timestamps, created_by, status,
        supersession pointers, receipt skeletons, state hashes — the audit /
        lineage plane — so the hash chain still verifies and every recorded run
        still byte-replays.  Issue the machine-verifiable proof with
        :meth:`erasure_certificate`.

        Production: additionally schedule KMS key deletion for a per-scope CMK.

        Returns a dict with shred summary (scope_key, shredded,
        relationships_affected, episodes_retired).
        """
        self._require_authorized_scope(scope)
        scope_key = scope.key
        # Count sealed relationship rows in this scope (audit).
        affected = sum(
            1
            for rel in self.graph.relationships()
            if rel.properties.get("scope_key") == scope_key and is_sealed_content(rel.properties.get("fact"))
        )
        # WS-12: retire pending evidence — RTBF means the erased raw episodes must
        # never be extracted; marking them processed removes them from every queue.
        episodes_retired = 0
        for episode in self.graph.episodes_for_scope(scope_key):
            if not self.graph.is_episode_processed(episode.uuid):
                self.graph.mark_episode_processed(episode.uuid, processed_at=datetime.now(UTC))
                episodes_retired += 1
        operator_run = self._begin_operator_run(job_name="crypto_shred", scope_key=scope_key)
        shredded_at = datetime.now(UTC)
        for relationship in self.graph.relationships():
            if (
                relationship.type != "MENTIONS"
                and relationship.properties.get("scope_key") == scope_key
                and relationship.properties.get("memory_type") != "rollup"
            ):
                self._engine.invalidate_rollups_for_dependency(
                    relationship_uuid=relationship.uuid,
                    reason="dependency_crypto_shredded",
                    now=shredded_at,
                    receipt_run=operator_run,
                )
        # A derived rollup can retain an erased child's semantic content even
        # when it is merely stale.  Hard-purge every in-scope derived artifact
        # before key destruction so it exits all retrieval surfaces
        # synchronously; each purge is bound into the erasure certificate.
        derived_artifacts_purged = 0
        for relationship in self.graph.relationships():
            if (
                relationship.properties.get("scope_key") != scope_key
                or relationship.properties.get("memory_type") != "rollup"
                or relationship.properties.get("status") != RelationshipStatus.ACTIVE.value
            ):
                continue
            before = self.graph.graph_state_hash(scope_key)
            self.graph.update_relationship(
                relationship.uuid,
                properties={
                    "status": RelationshipStatus.PRUNED.value,
                    "active_in_context": False,
                    "rollup_stale": True,
                    "erasure_purged": True,
                    "erasure_purged_at": shredded_at.isoformat(),
                },
                valid_to=shredded_at,
            )
            after = self.graph.graph_state_hash(scope_key)
            self._emit_receipt(
                operator_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_ERASURE_PURGED,
                decision_reason="dependency_crypto_shredded",
                decision_result="pruned",
                now=shredded_at,
                scope_key=scope_key,
                memory_type="rollup",
                relationship_type="ROLLUP",
                relationship_uuid=relationship.uuid,
                source_span_digest=str(relationship.properties.get("rollup_dependency_digest") or ""),
                graph_state_hash_before=before,
                graph_state_hash_after=after,
            )
            derived_artifacts_purged += 1
        shredded = self.graph.shred_governance_key(scope_key, subject_key)
        # WS-11: the shred itself is an auditable operator-run receipt.  Chains verify
        # after the key is destroyed because every hash is over stored (ciphertext)
        # representations, never the key (PATENT_REPLAY_RECEIPTS_SPEC claim 10, [0021]).
        # The key destruction moves no graph rows, so the receipt records a zero-delta
        # state-hash transition — the shred run itself byte-replays.
        shred_state_hash = self.graph.graph_state_hash(scope_key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.CRYPTO_SHRED_KEY_DESTROYED,
            decision_reason=(
                f"crypto_shred scope={scope_key} subject_key={subject_key!r} shredded={shredded} "
                f"episodes_retired={episodes_retired}"
            ),
            decision_result="destroyed",
            now=shredded_at,
            scope_key=scope_key,
            graph_state_hash_before=shred_state_hash,
            graph_state_hash_after=shred_state_hash,
        )
        self._checkpoint_operator_run(operator_run)
        return {
            "scope_key": scope_key,
            "subject_key": subject_key,
            "shredded": shredded,
            "relationships_affected": affected,
            "episodes_retired": episodes_retired,
            "derived_artifacts_purged": derived_artifacts_purged,
        }

    async def erasure_certificate(self, *, scope: MemoryScope) -> ErasureCertificate:
        """Issue the machine-verifiable erasure certificate for a shredded scope (WS-12).

        Fail-closed: the certificate exists only when ALL four legs verify —
        (1) the scope DEK is destroyed and the destruction is a receipted event,
        (2) the ciphertext-only sweep finds no plaintext content remnant in any
        covered store, (3) every receipt run that touched the scope still
        chain-verifies, and (4) every checkpointed mutating run still
        byte-replays.  The certificate digest is then appended to the SAME
        hash chain as an ``ERASURE_CERTIFICATE_ISSUED`` receipt, binding the
        proof into the tamper-evident sequence.  Re-verify any time with
        :meth:`verify_erasure`.
        """
        self._require_authorized_scope(scope)
        certificate = await issue_erasure_certificate(self.graph, scope_key=scope.key)
        operator_run = self._begin_operator_run(job_name="erasure_certificate", scope_key=scope.key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.ERASURE_CERTIFICATE_ISSUED,
            decision_reason=f"erasure_certificate_digest:{certificate.certificate_digest}",
            decision_result="recorded",
            now=certificate.issued_at,
            scope_key=scope.key,
        )
        self._checkpoint_operator_run(operator_run)
        return certificate

    async def verify_erasure(self, *, scope: MemoryScope, certificate: ErasureCertificate) -> bool:
        """Re-execute an erasure certificate against the live store (WS-12).

        Recomputes every leg the certificate asserts — key destruction,
        ciphertext-only sweep, chain verification, byte replay — plus the
        certificate's own digest and its issuance receipt.  Raises
        :class:`ErasureVerificationError` on any failure; returns True when the
        erasure proof still holds.
        """
        self._require_authorized_scope(scope)
        if certificate.scope_key != scope.key:
            raise ValueError(
                f"certificate scope {certificate.scope_key!r} does not match requested scope {scope.key!r}"
            )
        return await verify_erasure_certificate(self.graph, certificate=certificate)

    async def redact_relationship_version(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope | None = None,
        reason: str = "operator_pii_redact",
    ) -> dict[str, Any]:
        """Scrub PATTERN-MATCHED PII from one historical relationship, preserving audit trail.

        Anthropic redact-a-version pattern: the who/when/lineage audit trail
        (status, valid_from/to, created_by, superseded pointers, episode_uuids)
        is preserved; only the text content (fact, predicate) is redacted.

        What is actually removed, and what is not
        -----------------------------------------
        Redaction is :class:`~memotron.redaction.RedactionStrategy.REDACT`, which is
        **pattern-based**. It removes what it can express as a pattern and nothing else.
        Measured 2026-09-02:

            'contact priya@example.com about it'      -> 'contact [REDACTED] about it'
            'call 555-867-5309 tomorrow'              -> 'call [REDACTED] tomorrow'
            'SSN 123-45-6789 on file'                 -> 'SSN [REDACTED] on file'
            'card 4111 1111 1111 1111 saved'          -> 'card [REDACTED] saved'
            'Priya Sharma prefers quarterly reviews'  -> UNCHANGED

        **A person's name is not redacted.** Neither is an address, an employer, a job
        title, or any other identifier with no lexical shape to match on. There is no
        entity recognition here.

        ``{"redacted": True}`` in the result means *this call ran and rewrote the stored
        version*. It does **not** mean the text no longer identifies anyone, and it does
        not report what was matched. An operator asked to scrub a name, calling this and
        reading that flag, will believe a name is gone when it is still there and still
        readable — which is why this paragraph exists rather than a docstring that says
        only "scrub PII" (T3-12, #118).

        For a name, the operator wants supersession or crypto-shred, not redaction.

        Returns a dict describing the redacted relationship.
        """
        if not relationship_uuid.strip():
            raise ValueError("relationship_uuid cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        relationship = self.graph.get_relationship(relationship_uuid)
        relationship_scope = self._relationship_scope(relationship.properties)
        self._require_authorized_scope(relationship_scope)
        if scope is not None and relationship_scope != scope:
            raise ValueError(f"relationship scope {relationship_scope.key} does not match requested scope {scope.key}")
        from memotron.redaction import RedactionStrategy, Redactor

        redactor = Redactor(RedactionStrategy.REDACT)
        stored_fact = relationship.properties.get("fact", "")
        # WS-12: a crypto-shred row stores its fact sealed — reveal, redact, and
        # re-seal (with a refreshed keyed commitment) so the redacted version is
        # still ciphertext-only at rest.  A shredded scope cannot be redacted:
        # its content is already unrecoverable.
        content_key = None
        if is_sealed_content(stored_fact):
            content_key = self.graph.get_governance_key(relationship_scope.key)
            if content_key is None:
                raise ValueError(
                    f"relationship {relationship_uuid} belongs to crypto-shredded scope "
                    f"{relationship_scope.key}; its content is already unrecoverable"
                )
        current_fact = str(self.graph.reveal(relationship_scope.key, stored_fact))
        current_predicate = str(relationship.properties.get("predicate", ""))
        redacted_fact = redactor.redact_text(current_fact)
        redacted_predicate = redactor.redact_text(current_predicate)
        stored_redacted_fact: Any = redacted_fact
        redaction_updates: dict[str, Any] = {}
        if content_key is not None:
            stored_redacted_fact = seal_content(redacted_fact, content_key)
            redaction_updates["fact_commitment"] = content_commitment(content_key, "fact", redacted_fact)
        self.graph.update_relationship(
            relationship_uuid,
            properties={
                "fact": stored_redacted_fact,
                "predicate": redacted_predicate,
                "pii_redacted": True,
                "pii_redact_reason": normalized_reason,
                "pii_redacted_at": datetime.now(UTC).isoformat(),
                **redaction_updates,
            },
        )
        # WS-11: version-redaction operator receipt with before/after fact digests ([0021]).
        import hashlib

        operator_run = self._begin_operator_run(
            job_name="redact_relationship_version", scope_key=relationship_scope.key
        )
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.REDACTION_RELATIONSHIP_VERSION_REDACTED,
            decision_reason=normalized_reason,
            decision_result="transformed",
            now=datetime.now(UTC),
            scope_key=relationship_scope.key,
            relationship_uuid=relationship_uuid,
            relationship_type=relationship.type,
            memory_type=self._engine.memory_type_for_relationship(dict(relationship.properties)),
            redaction_digest_before=hashlib.sha256(current_fact.encode("utf-8")).hexdigest(),
            redaction_digest_after=hashlib.sha256(redacted_fact.encode("utf-8")).hexdigest(),
        )
        self._checkpoint_operator_run(operator_run)
        return {
            "relationship_uuid": relationship_uuid,
            "scope_key": relationship_scope.key,
            "redacted": True,
            "reason": normalized_reason,
        }

    def _ingest_governance(self, metadata: dict[str, Any]) -> GovernancePolicy | None:
        """WS-12: governance in force at ingest time (Motive hint > config default).

        An unknown motive hint is ignored here — formation fails fast on it
        later; ingest must not change that contract.
        """
        motive_name = metadata.get("motive")
        if isinstance(motive_name, str) and self.config.memory_bank is not None:
            try:
                motive = self.config.memory_bank.motive(motive_name)
            except ValueError:
                motive = None
            if motive is not None and motive.governance is not None:
                return motive.governance
        return self.config.governance
