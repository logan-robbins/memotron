"""Finding archived memories and bringing them back.

Restore is the counterweight to every destructive-looking operation in the client:
forget archives rather than deletes, so `restore_archived_memory` is what makes that
claim true. `_restore_ghost_matches` handles the harder case -- a row pruned with a
ghost recorded, where the ghost is the only evidence the memory existed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.client._protocol import ComposedMemotron
from memotron.crypto import ContentKeyUnavailableError
from memotron.embedding import (
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.models import (
    ArchivedMatchDisposition,
    ArchivedMatchReport,
    ArchivedMemoryMatch,
    DreamDecisionRecord,
    DreamJobKind,
    MemoryScope,
    RestoreArchivedMemoryResult,
)
from memotron.receipts import (
    ReceiptDecisionType,
)
from memotron.storage import (
    StorageBackend,
    normalize_key,
)

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class ArchiveMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    def _scope_archived_matches(
        self,
        *,
        scope: MemoryScope,
        query: str,
        semantic: bool,
        reader_agent_id: str | None = None,
    ) -> None:
        """Restore an archived row only when a live retrieval asks for it.

        Ghosts are an archive-tier recovery mechanism, not an alternate search
        index.  A crypto-shredded row fails closed: its ghost becomes
        non-restorable and contributes evidence of erasure rather than a path
        back into runtime context.

        WS-23 C6: restoration is reader-aware.  It runs BEFORE the retrieval
        visibility filter, so an agent the row's ``visibility_agents`` allowlist
        excludes used to resurrect it anyway — no content reached that agent,
        but it was an unauthorized state mutation, and the restore writes a
        plaintext dream-decision record naming the confidential fact.  A row the
        caller may not view is skipped; the operator / SDK-owner reader
        (``None``) is unaffected.
        """
        query_tokens = set(normalize_key(query).split())
        query_embedding: list[float] | None = None
        # WS-23 H1: a content-protected scope is pinned to the hermetic local
        # transport, so a sealed query is never re-embedded through a network
        # endpoint and stored/query vectors stay same-space.
        embedding_transport = self._engine.content_embedding_transport(scope_key=scope.key)
        if semantic:
            query_embedding = embedding_transport.embed(query)
        matches: list[ArchivedMemoryMatch] = []
        for ghost in self.graph.prune_ghosts(scope_key=scope.key, restorable_only=True):
            relationship = self.graph.get_relationship(ghost.relationship_uuid)
            if not self._agent_may_view_relationship(relationship.properties, reader_agent_id):
                continue
            try:
                fact = str(self.graph.reveal(scope.key, relationship.properties.get("fact", "")))
            except ContentKeyUnavailableError:
                continue
            matched = False
            if semantic:
                # WS-17 T18 vector-space guard: never cosine a ghost's stored
                # vector against a query embedded in a different space.
                stored_embedding = (
                    self.graph.reveal_vector(scope.key, relationship.properties.get("embedding"))
                    if stored_vector_in_active_space(
                        relationship.properties,
                        active_identifier=embedding_transport.identifier,
                    )
                    else None
                )
                if stored_embedding is not None and query_embedding is not None:
                    try:
                        matched = cosine_similarity(query_embedding, list(stored_embedding)) > 0.0
                    except ValueError:
                        matched = False
            else:
                matched = bool(query_tokens.intersection(set(normalize_key(fact).split())))
            if not matched:
                continue
            matches.append(
                ArchivedMemoryMatch(
                    relationship_uuid=relationship.uuid,
                    scope=scope,
                    fact=fact,
                    prune_receipt_uuid=ghost.prune_receipt_uuid,
                    pruned_at=ghost.pruned_at,
                    pruned_reason=ghost.reason,
                    restorable=ghost.restorable,
                    retrieval_mode="semantic" if semantic else "keyword",
                )
            )
        return matches

    def archived_matches(
        self,
        *,
        query: str,
        scope: MemoryScope | None = None,
        scopes: list[MemoryScope] | None = None,
        semantic: bool = False,
    ) -> ArchivedMatchReport:
        """Report archived memories a query would match — without reviving them.

        Always a pure read, in both retrieval modes, so a caller can ask "is
        there anything in the archive tier for this query?" without mutating
        anything.  The returned report carries
        ``disposition == "available_to_restore"``: nothing here was revived.
        (``search``/``semantic_search`` return the same shape of report on
        their result object, with ``disposition == "revived"`` under the
        default revive-on-read behaviour.)

        Pass exactly one of ``scope`` or ``scopes``.  Restoring one of the
        reported rows requires the explicit :meth:`restore_archived_memory`
        curation action.
        """
        # T3-8: this reads archived (crypto-shredded) content for whatever scope it is
        # handed. Guarded ahead of the exactly-one-of check so authorization fails closed
        # before argument validation. No-op unless the client set authorized_scope_keys.
        self._require_authorized_scope(scope, *(scopes or []))
        if (scope is None) == (scopes is None):
            raise ValueError("pass exactly one of scope or scopes")
        report_scopes: list[MemoryScope] = list(scopes) if scopes is not None else [scope]
        mode = "semantic" if semantic else "keyword"
        matches: list[ArchivedMemoryMatch] = []
        if query.strip():
            for candidate in report_scopes:
                matches.extend(self._scope_archived_matches(scope=candidate, query=query, semantic=semantic))
        return ArchivedMatchReport(
            query=query,
            scope_keys=[candidate.key for candidate in report_scopes],
            retrieval_mode=mode,
            count=len(matches),
            matches=matches,
            disposition=ArchivedMatchDisposition.AVAILABLE_TO_RESTORE,
        )

    def _archived_match_disposition(self) -> ArchivedMatchDisposition:
        """Which disposition this client's retrieval reports its matches with."""
        return (
            ArchivedMatchDisposition.AVAILABLE_TO_RESTORE
            if self.config.pure_read_retrieval
            else ArchivedMatchDisposition.REVIVED
        )

    def _empty_archived_report(
        self,
        *,
        query: str,
        scopes: list[MemoryScope],
        semantic: bool,
    ) -> ArchivedMatchReport:
        """An empty report for a retrieval that does not consult the archive tier."""
        return ArchivedMatchReport(
            query=query,
            scope_keys=[candidate.key for candidate in scopes],
            retrieval_mode="semantic" if semantic else "keyword",
            disposition=self._archived_match_disposition(),
        )

    async def _retrieval_archived_report(
        self,
        *,
        query: str,
        scopes: list[MemoryScope],
        semantic: bool,
        reader_agent_id: str | None = None,
    ) -> ArchivedMatchReport:
        """The archive-tier half of a retrieval, in whichever mode is configured.

        Default (``DreamConfig.pure_read_retrieval`` is False): revive every
        matching archived row inline — the historical behaviour, receipts and
        all — and report the rows that came back.

        Flag on: report the matching rows and write nothing, so retrieval takes
        no row locks and can be served by more than one replica.
        """
        if self.config.pure_read_retrieval:
            return self.archived_matches(query=query, scopes=list(scopes), semantic=semantic)
        matches: list[ArchivedMemoryMatch] = []
        if query.strip():
            for candidate in scopes:
                matches.extend(
                    await self._restore_ghost_matches(
                        scope=candidate,
                        query=query,
                        semantic=semantic,
                        reader_agent_id=reader_agent_id,
                    )
                )
        return ArchivedMatchReport(
            query=query,
            scope_keys=[candidate.key for candidate in scopes],
            retrieval_mode="semantic" if semantic else "keyword",
            count=len(matches),
            matches=matches,
            disposition=ArchivedMatchDisposition.REVIVED,
        )

    async def restore_archived_memory(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope | None = None,
        reason: str = "curation_restore_requested",
        requested_by: str = "curation",
        now: datetime | None = None,
    ) -> RestoreArchivedMemoryResult:
        """Revive one archived memory — the explicit curation action.

        Additive, and available in **both** retrieval modes.  By default
        retrieval still revives a matching archived row as a side effect of
        reading; this is how an agent or operator asks for a specific archived
        memory back without a query that happens to match it.  It is the *only*
        way back when ``DreamConfig.pure_read_retrieval`` is on, where
        ``search``/``semantic_search`` are pure reads that merely report
        archived matches (``results.archived``) — which is what lets retrieval
        run without taking row locks, and therefore on more than one replica.

        The audit trail matches the read path's in content: the same
        ``PRUNING_GHOST_RESTORED`` receipt (state-hash bracketed, carrying the
        prune receipt uuid and the cohort ghost-regret telemetry) and the same
        ``pruning_ghost_regret`` dream decision, attributed to curation rather
        than to ``runtime-retrieval``.

        Fails closed, exactly as before:

        * a crypto-shredded ghost raises ``ContentKeyUnavailableError`` and is
          durably demoted to non-restorable by the storage layer;
        * a ghost already marked non-restorable raises ``ValueError``;
        * an unknown relationship raises ``ValueError``.
        """
        # T3-8: a WRITE -- it un-archives a row. The pre-existing check that the row's
        # scope matches the requested one is not authorization: it stops you naming a row
        # from a DIFFERENT scope, not from touching a scope you were never granted.
        self._require_authorized_scope(scope)
        normalized_uuid = relationship_uuid.strip()
        if not normalized_uuid:
            raise ValueError("relationship_uuid cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        normalized_requested_by = requested_by.strip() or "curation"
        ghost = self.graph.prune_ghost(normalized_uuid)
        ghost_scope = ghost.scope
        # WS-19 T21: guard the RESOLVED ghost scope, the same way memory_evidence and
        # forget_memory guard their resolved relationship scope. The guard at the top of
        # this method takes the REQUESTED scope, which is None when the caller omits it --
        # and omitting it was a live bypass: a scope-guarded client could un-archive a row
        # in a scope it was never granted, while passing that same scope explicitly was
        # refused. A write, so it was privilege escalation rather than a leak.
        self._require_authorized_scope(ghost_scope)
        if scope is not None and ghost_scope != scope:
            raise ValueError(f"relationship scope {ghost_scope.key} does not match requested scope {scope.key}")
        relationship = self.graph.get_relationship(normalized_uuid)
        previous_status = self._relationship_status(relationship.properties.get("status"))
        restored_at = now or datetime.now(UTC)
        before = self.graph.graph_state_hash(ghost_scope.key)
        # The crypto-shred guard and the "not restorable" refusal live in the
        # storage layer so both engines behave identically; an explicit request
        # surfaces them to the caller instead of silently skipping.
        restored = self.graph.restore_prune_ghost(normalized_uuid, restored_at=restored_at)
        after = self.graph.graph_state_hash(ghost_scope.key)
        restored_relationship = self.graph.get_relationship(normalized_uuid)
        fact = str(self.graph.reveal(ghost_scope.key, restored_relationship.properties.get("fact", "")))
        effective_restored_at = restored.restored_at or restored_at
        # Regret telemetry preserved verbatim from the old read path: the share
        # of this scope's ghosts that have been restored, and whether that
        # crosses the configured alert rate.
        all_ghosts = self.graph.prune_ghosts(scope_key=ghost_scope.key)
        restored_count = sum(ghost_row.restored_at is not None for ghost_row in all_ghosts)
        regret_rate = restored_count / len(all_ghosts) if all_ghosts else 0.0
        policy = self.config.pruning.retention
        regret_alert = regret_rate > policy.ghost_regret_alert_rate
        run = self._begin_operator_run(job_name="restore_prune_ghost", scope_key=ghost_scope.key)
        receipt = self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.PRUNING_GHOST_RESTORED,
            decision_reason=normalized_reason,
            decision_result="materialized",
            now=effective_restored_at,
            scope_key=ghost_scope.key,
            relationship_uuid=restored_relationship.uuid,
            relationship_type=restored_relationship.type,
            graph_state_hash_before=before,
            graph_state_hash_after=after,
            retention_components=json.dumps(
                {
                    "ghost_prune_receipt_uuid": restored.prune_receipt_uuid,
                    "ghost_regret_rate": regret_rate,
                    "ghost_regret_alert": regret_alert,
                    # Was "retrieval_mode" while retrieval restored inline.
                    "restore_mode": "explicit_curation",
                    "restore_requested_by": normalized_requested_by,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self._checkpoint_operator_run(run)
        self.graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=effective_restored_at,
                job_name="restore_prune_ghost",
                job_kind=DreamJobKind.PRUNING,
                agent_id="curation-restore",
                agent_name="curation-restore",
                agent_scope=ghost_scope,
                decision_type="pruning_ghost_regret",
                summary=(
                    f"Curation restored archived relationship {restored_relationship.uuid}; "
                    f"ghost regret is {regret_rate:.3f}."
                ),
                subject_id=restored_relationship.uuid,
                subject_name=fact,
                scope=ghost_scope,
                details={
                    "prune_receipt_uuid": restored.prune_receipt_uuid,
                    "restore_receipt_uuid": receipt.receipt_uuid,
                    "ghost_regret_rate": regret_rate,
                    "ghost_regret_alert": regret_alert,
                    "restore_mode": "explicit_curation",
                    "restore_requested_by": normalized_requested_by,
                    "restore_reason": normalized_reason,
                },
            )
        )
        return RestoreArchivedMemoryResult(
            relationship_uuid=restored_relationship.uuid,
            scope=ghost_scope,
            fact=fact,
            previous_status=previous_status,
            status=self._relationship_status(restored_relationship.properties.get("status")),
            restored_at=effective_restored_at,
            reason=normalized_reason,
            requested_by=normalized_requested_by,
            prune_receipt_uuid=restored.prune_receipt_uuid,
            restore_receipt_uuid=receipt.receipt_uuid,
            ghost_regret_rate=regret_rate,
            ghost_regret_alert=regret_alert,
        )

    async def _restore_ghost_matches(
        self,
        *,
        scope: MemoryScope,
        query: str,
        semantic: bool,
        reader_agent_id: str | None = None,
    ) -> list[ArchivedMemoryMatch]:
        """Restore an archived row when a live retrieval asks for it — the default.

        Ghosts are an archive-tier recovery mechanism, not an alternate search
        index.  A crypto-shredded row fails closed: its ghost becomes
        non-restorable and contributes evidence of erasure rather than a path
        back into runtime context.

        Reached only from :meth:`_retrieval_archived_report` with
        ``DreamConfig.pure_read_retrieval`` off (the default), where it is the
        historical revive-on-read behaviour: the same
        ``PRUNING_GHOST_RESTORED`` receipt, the same ``pruning_ghost_regret``
        dream decision, the same telemetry.  Returns the rows it actually
        revived so retrieval can report them on ``results.archived``.
        """
        revived: list[ArchivedMemoryMatch] = []
        for match in self._scope_archived_matches(scope=scope, query=query, semantic=semantic):
            relationship = self.graph.get_relationship(match.relationship_uuid)
            # WS-23 C6: restoration is reader-aware — a row the caller may not
            # view is skipped (no unauthorized state mutation); the operator /
            # SDK-owner reader (``None``) is unaffected.
            if not self._agent_may_view_relationship(relationship.properties, reader_agent_id):
                continue
            fact = match.fact
            before = self.graph.graph_state_hash(scope.key)
            try:
                restored = self.graph.restore_prune_ghost(relationship.uuid, restored_at=datetime.now(UTC))
            except ContentKeyUnavailableError:
                # restore_prune_ghost marks the ghost non-restorable before it raises.
                continue
            after = self.graph.graph_state_hash(scope.key)
            all_ghosts = self.graph.prune_ghosts(scope_key=scope.key)
            restored_count = sum(ghost_row.restored_at is not None for ghost_row in all_ghosts)
            regret_rate = restored_count / len(all_ghosts) if all_ghosts else 0.0
            policy = self.config.pruning.retention
            run = self._begin_operator_run(job_name="restore_prune_ghost", scope_key=scope.key)
            receipt = self._emit_receipt(
                run,
                decision_type=ReceiptDecisionType.PRUNING_GHOST_RESTORED,
                decision_reason="retrieval_matched_prune_ghost",
                decision_result="materialized",
                now=restored.restored_at or datetime.now(UTC),
                scope_key=scope.key,
                relationship_uuid=relationship.uuid,
                relationship_type=relationship.type,
                graph_state_hash_before=before,
                graph_state_hash_after=after,
                retention_components=json.dumps(
                    {
                        "ghost_prune_receipt_uuid": restored.prune_receipt_uuid,
                        "ghost_regret_rate": regret_rate,
                        "ghost_regret_alert": regret_rate > policy.ghost_regret_alert_rate,
                        "retrieval_mode": "semantic" if semantic else "keyword",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            self._checkpoint_operator_run(run)
            self.graph.record_dream_decision(
                DreamDecisionRecord(
                    ran_at=restored.restored_at or datetime.now(UTC),
                    job_name="restore_prune_ghost",
                    job_kind=DreamJobKind.PRUNING,
                    agent_id="runtime-retrieval",
                    agent_name="runtime-retrieval",
                    agent_scope=scope,
                    decision_type="pruning_ghost_regret",
                    summary=(
                        f"Retrieval restored archived relationship {relationship.uuid}; "
                        f"ghost regret is {regret_rate:.3f}."
                    ),
                    subject_id=relationship.uuid,
                    subject_name=fact,
                    scope=scope,
                    details={
                        "prune_receipt_uuid": restored.prune_receipt_uuid,
                        "restore_receipt_uuid": receipt.receipt_uuid,
                        "ghost_regret_rate": regret_rate,
                        "ghost_regret_alert": regret_rate > policy.ghost_regret_alert_rate,
                    },
                )
            )
            revived.append(match.model_copy(update={"revived": True}))
        return revived
