"""Everything that puts something INTO the graph.

Seven public methods and 545 lines, `add_memory` alone 187. This is the operator
write path -- the one place a caller states a fact directly rather than having one
extracted -- so it is also the path that must fail fast rather than quarantine.
_materialize_episode's `raw_candidates_stored=False` regime exists for these callers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    Motive,
)
from memotron.models import (
    AddArtifactResult,
    AddContextResult,
    AddEpisodeResult,
    AddMemoryResult,
    AddSessionResult,
    ConversationTurn,
    Episode,
    EpisodeType,
    MemoryScope,
)
from memotron.multimodal import Artifact, ArtifactProvenance, LocalMultimodalNormalizer, MultimodalNormalizer
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class IngestMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def add_memory(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        relationship_type: str,
        scope: MemoryScope,
        confidence: float = 1.0,
        name: str | None = None,
        source_description: str = "client-managed memory",
        reference_time: datetime | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        subject_label: str = "Entity",
        object_label: str = "Entity",
        subject_properties: dict[str, Any] | None = None,
        object_properties: dict[str, Any] | None = None,
        source_text: str | None = None,
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
        motive: Motive | str | None = None,
        saves_step: str | None = None,
    ) -> AddMemoryResult:
        """Materialize one caller-supplied memory immediately.

        The supplied fact is validated against the instruction set and written
        through the same graph materialization path used by dreamed memories.
        A processed evidence episode is stored for audit, so formation jobs do
        not extract it again.
        """
        self._check_scope_not_read_only(scope)
        instructions = self.config.instruction_set(instruction_set)
        normalized_subject = subject.strip()
        normalized_predicate = predicate.strip()
        normalized_object = object.strip()
        if not normalized_subject:
            raise ValueError("subject cannot be blank")
        if not normalized_predicate:
            raise ValueError("predicate cannot be blank")
        if not normalized_object:
            raise ValueError("object cannot be blank")
        if not source_description.strip():
            raise ValueError("source_description cannot be blank")

        reference = reference_time or valid_from or datetime.now(UTC)
        if name is not None and not name.strip():
            raise ValueError("name cannot be blank")

        trace_metadata = {
            **(metadata or {}),
            "memory_mode": "client_managed",
            "client_managed_memory": True,
        }
        trace_name = (
            name.strip()
            if name is not None
            else (f"client memory: {normalized_subject} {normalized_predicate} {normalized_object}")
        )
        trace_episode = Episode(
            name=trace_name,
            body="{}",
            source=EpisodeType.JSON,
            source_description=source_description.strip(),
            scope=scope,
            reference_time=reference,
            metadata=trace_metadata,
            instruction_set=instruction_set,
        )
        memory = self._extractor.validate_memory(
            {
                "subject": normalized_subject,
                "predicate": normalized_predicate,
                "object": normalized_object,
                "relationship_type": relationship_type,
                "confidence": confidence,
                "subject_label": subject_label,
                "object_label": object_label,
                "subject_properties": dict(subject_properties or {}),
                "object_properties": dict(object_properties or {}),
                "valid_from": valid_from,
                "valid_to": valid_to,
                "scope": scope,
                "source_text": source_text,
                "metadata": trace_metadata,
                # WS-24: an operator writing one fact by hand HAS decided it is
                # worth remembering, so the actionability gate is not applied
                # here; the stated step is recorded when supplied because the
                # justification is worth keeping next to the fact regardless.
                "saves_step": saves_step,
            },
            episode=trace_episode,
            instructions=instructions,
        )
        resolved_motive: Motive | None
        if isinstance(motive, Motive):
            resolved_motive = motive
        elif isinstance(motive, str):
            if self.config.memory_bank is None:
                raise ValueError(f"motive {motive!r} requested but no MemoryBank is configured")
            resolved_motive = self.config.memory_bank.motive(motive)
        else:
            resolved_motive = None
        relationship_instruction = next(
            item for item in instructions.relationship_instructions if item.type == memory.relationship_type
        )
        if (
            resolved_motive is not None
            and resolved_motive.allowed_memory_types
            and relationship_instruction.memory_type not in resolved_motive.allowed_memory_types
        ):
            raise ValueError(
                f"motive {resolved_motive.name!r} does not allow memory type "
                f"{relationship_instruction.memory_type.value!r}"
            )
        # Reject raw credential material before the evidence episode itself is
        # persisted.  The rejection receipt binds only a digest, never the raw
        # value, and the caller gets a fail-fast error.
        operator_run = self._begin_operator_run(job_name="add_memory", scope_key=(memory.scope or scope).key)
        try:
            self._engine._secret_reference_metadata(memory)
        except ValueError as exc:
            self._engine._reject_raw_secret_memory(
                receipt_run=operator_run,
                episode=trace_episode,
                memory=memory,
                now=reference,
                motive_name=resolved_motive.name if resolved_motive is not None else None,
                governance_policy_digest=None,
                protection=None,
                error=exc,
            )
            self._checkpoint_operator_run(operator_run)
            raise
        trace_episode.body = json.dumps(
            {"memories": [memory.model_dump(mode="json", exclude_none=True)]},
            sort_keys=True,
        )
        trace_episode = self._store_episode(trace_episode)
        # WS-11: client-managed writes materialize under a 1-episode operator receipt run.
        _, created_relationships, reinforced, superseded = self._engine.materialize_episode(
            trace_episode,
            [memory],
            created_by="client-managed-memory",
            receipt_run=operator_run,
            now=reference,
            dedup_threshold_override=(resolved_motive.dedup_threshold if resolved_motive is not None else None),
            governance=(resolved_motive.governance if resolved_motive is not None else None),
            motive_name=resolved_motive.name if resolved_motive is not None else None,
        )
        self._checkpoint_operator_run(operator_run)
        self.graph.mark_episode_processed(trace_episode.uuid, processed_at=datetime.now(UTC))
        relationship = self._materialized_memory_relationship(
            scope=memory.scope or scope,
            subject=memory.subject,
            predicate=memory.predicate,
            object_value=memory.object,
            relationship_type=memory.relationship_type,
            episode_uuid=trace_episode.uuid,
        )
        return AddMemoryResult(
            episode_uuid=trace_episode.uuid,
            relationship_uuid=relationship.uuid,
            scope=memory.scope or scope,
            fact=str(
                self.graph.reveal(
                    (memory.scope or scope).key,
                    relationship.properties.get("fact", f"{memory.subject} {memory.predicate} {memory.object}"),
                )
            ),
            created_relationships=created_relationships,
            reinforced_relationships=reinforced,
            superseded_relationships=superseded,
        )

    async def add_context(
        self,
        *,
        name: str,
        content: str,
        scopes: list[MemoryScope],
        source_description: str = "shadow context",
        reference_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        custom_id: str | None = None,
        source: EpisodeType = EpisodeType.TEXT,
        instruction_set: str = "default",
        max_chars_per_episode: int = 4000,
        motive: str | None = None,
        trusted: bool = True,
    ) -> AddContextResult:
        """Ingest a raw document, chunking it into episodes for offline formation.

        Parameters
        ----------
        motive:
            WS-3 per-episode Motive hint.  When set, stamps ``metadata["motive"]``
            on every episode chunk so the formation job can select the named Motive.
        trusted:
            WS-7: When False, stamps ``metadata["trusted"] = False`` on every episode
            chunk, enabling the untrusted-directive gate in formation.
        """
        for scope in scopes:
            self._check_scope_not_read_only(scope)
        self.config.instruction_set(instruction_set)
        if not name.strip():
            raise ValueError("name cannot be blank")
        if not content.strip():
            raise ValueError("content cannot be blank")
        if not scopes:
            raise ValueError("at least one scope is required")
        if max_chars_per_episode <= 0:
            raise ValueError("max_chars_per_episode must be greater than zero")

        chunks = self._chunk_context(content, max_chars_per_episode)
        document_id = custom_id.strip() if custom_id and custom_id.strip() else str(uuid4())
        reference = reference_time or datetime.now(UTC)
        base_metadata = dict(metadata or {})
        if motive is not None:
            base_metadata["motive"] = motive
        if not trusted:
            base_metadata["trusted"] = False
        episode_uuids: list[str] = []
        for scope in scopes:
            for chunk_index, chunk in enumerate(chunks):
                episode = Episode(
                    name=self._context_episode_name(
                        name=name,
                        scope=scope,
                        chunk_index=chunk_index,
                        chunk_count=len(chunks),
                    ),
                    body=chunk,
                    source=source,
                    source_description=source_description,
                    scope=scope,
                    reference_time=reference,
                    metadata={
                        **base_metadata,
                        "shadow_ingest": True,
                        "shadow_document_id": document_id,
                        "shadow_custom_id": custom_id,
                        "shadow_source_name": name,
                        "shadow_scope_key": scope.key,
                        "shadow_chunk_index": chunk_index,
                        "shadow_chunk_count": len(chunks),
                        "shadow_original_length": len(content),
                        "shadow_max_chars_per_episode": max_chars_per_episode,
                    },
                    instruction_set=instruction_set,
                )
                self._store_episode(episode)
                episode_uuids.append(episode.uuid)
        return AddContextResult(
            document_id=document_id,
            scope_keys=[scope.key for scope in scopes],
            chunks_created=len(chunks),
            episodes_created=len(episode_uuids),
            episode_uuids=episode_uuids,
            queued_for_dreaming=True,
        )

    async def add_artifact(
        self,
        *,
        artifact: Artifact,
        scopes: list[MemoryScope],
        name: str | None = None,
        source_description: str = "multimodal artifact",
        reference_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
        motive: str | None = None,
        trusted: bool = True,
        normalizer: MultimodalNormalizer | None = None,
        max_chars_per_episode: int = 4000,
    ) -> AddArtifactResult:
        """Ingest a multimodal artifact by normalizing it to text before dreaming.

        This is WS-9's public ingestion surface.  The artifact is converted to a
        plain-text representation via the normalizer (default:
        ``LocalMultimodalNormalizer``), then chunked and queued as episodes exactly
        like ``add_context`` — so the existing dream pipeline extracts from it
        unchanged.

        Provenance fields (``artifact_id``, ``artifact_type``, ``artifact_location``,
        ``artifact_checksum``) are stamped on every episode's metadata so that
        ``memory_evidence()`` on any resulting graph fact reveals the originating
        artifact and its source location.

        Parameters
        ----------
        artifact:
            The multimodal artifact to ingest.
        scopes:
            One or more memory scopes.
        name:
            Human-readable episode name prefix (defaults to the artifact modality
            + artifact_id).
        source_description:
            Episode source description label.
        reference_time:
            Episode reference time (defaults to now).
        metadata:
            Additional caller metadata merged with provenance metadata.
        instruction_set:
            Instruction set name for the formation job.
        motive:
            WS-3 per-episode Motive hint.
        trusted:
            WS-7 trust flag (False = untrusted-directive gate).
        normalizer:
            Optional normalizer override; defaults to ``LocalMultimodalNormalizer``.
        max_chars_per_episode:
            Maximum characters per episode chunk (same contract as ``add_context``).
        """
        for scope in scopes:
            self._check_scope_not_read_only(scope)
        self.config.instruction_set(instruction_set)
        if not scopes:
            raise ValueError("at least one scope is required")
        if max_chars_per_episode <= 0:
            raise ValueError("max_chars_per_episode must be greater than zero")

        # Normalize the artifact to text.
        active_normalizer: MultimodalNormalizer = normalizer or LocalMultimodalNormalizer()
        normalized_text: str = active_normalizer.normalize(artifact)
        if not normalized_text.strip():
            raise ValueError(
                f"Normalizer produced empty text for {artifact.modality!r} artifact "
                f"(artifact_id={artifact.provenance().artifact_id!r})"
            )

        provenance: ArtifactProvenance = artifact.provenance()

        # Build episode metadata: provenance + caller metadata + WS-3/WS-7 flags.
        provenance_meta: dict[str, Any] = provenance.as_metadata()
        base_metadata: dict[str, Any] = {
            **provenance_meta,
            **(metadata or {}),
        }
        if motive is not None:
            base_metadata["motive"] = motive
        if not trusted:
            base_metadata["trusted"] = False

        # Derive episode name.
        episode_name_prefix: str = (
            name.strip() if name and name.strip() else f"artifact:{artifact.modality}:{provenance.artifact_id}"
        )

        reference = reference_time or datetime.now(UTC)
        chunks = self._chunk_context(normalized_text, max_chars_per_episode)
        episode_uuids: list[str] = []

        for scope in scopes:
            for chunk_index, chunk in enumerate(chunks):
                chunk_name = self._context_episode_name(
                    name=episode_name_prefix,
                    scope=scope,
                    chunk_index=chunk_index,
                    chunk_count=len(chunks),
                )
                episode_metadata: dict[str, Any] = {
                    **base_metadata,
                    "multimodal_ingest": True,
                    "shadow_ingest": True,
                    "shadow_document_id": provenance.artifact_id,
                    "shadow_custom_id": provenance.artifact_id,
                    "shadow_source_name": episode_name_prefix,
                    "shadow_scope_key": scope.key,
                    "shadow_chunk_index": chunk_index,
                    "shadow_chunk_count": len(chunks),
                    "shadow_original_length": len(normalized_text),
                    "shadow_max_chars_per_episode": max_chars_per_episode,
                }
                episode = Episode(
                    name=chunk_name,
                    body=chunk,
                    source=EpisodeType.TEXT,
                    source_description=source_description,
                    scope=scope,
                    reference_time=reference,
                    metadata=episode_metadata,
                    instruction_set=instruction_set,
                )
                self._store_episode(episode)
                episode_uuids.append(episode.uuid)

        return AddArtifactResult(
            artifact_id=provenance.artifact_id,
            artifact_type=provenance.artifact_type,
            artifact_location=provenance.artifact_location,
            normalized_text_length=len(normalized_text),
            episode_uuids=episode_uuids,
            scope_keys=[scope.key for scope in scopes],
            queued_for_dreaming=True,
        )

    async def add_artifacts(
        self,
        *,
        artifacts: list[Artifact],
        scopes: list[MemoryScope],
        source_description: str = "multimodal artifact",
        reference_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
        motive: str | None = None,
        trusted: bool = True,
        normalizer: MultimodalNormalizer | None = None,
        max_chars_per_episode: int = 4000,
    ) -> list[AddArtifactResult]:
        """Bulk variant of ``add_artifact``.  Ingests each artifact in order."""
        results: list[AddArtifactResult] = []
        for artifact in artifacts:
            result = await self.add_artifact(
                artifact=artifact,
                scopes=scopes,
                source_description=source_description,
                reference_time=reference_time,
                metadata=metadata,
                instruction_set=instruction_set,
                motive=motive,
                trusted=trusted,
                normalizer=normalizer,
                max_chars_per_episode=max_chars_per_episode,
            )
            results.append(result)
        return results

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source: EpisodeType,
        scope: MemoryScope,
        source_description: str = "",
        reference_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
        motive: str | None = None,
        trusted: bool = True,
    ) -> AddEpisodeResult:
        """Queue a caller-defined episode for offline formation.

        Parameters
        ----------
        motive:
            WS-3 per-episode Motive hint.  When set, stamps ``metadata["motive"]``
            so the formation job can select the named Motive for this episode.
            The name is not validated here — validation occurs when the formation
            job resolves the bank; unknown names will raise at formation time.
            Ignored when the config has no memory_bank or the job pins its own motive.
        trusted:
            WS-7: When False, stamps ``metadata["trusted"] = False`` on the episode
            so the untrusted-directive gate in formation can inspect it.
            Default True = fully trusted (legacy behaviour unchanged).
        """
        self._check_scope_not_read_only(scope)
        self.config.instruction_set(instruction_set)
        episode_metadata = dict(metadata or {})
        if motive is not None:
            episode_metadata["motive"] = motive
        if not trusted:
            episode_metadata["trusted"] = False
        episode = Episode(
            name=name,
            body=episode_body,
            source=source,
            source_description=source_description,
            scope=scope,
            reference_time=reference_time or datetime.now(UTC),
            metadata=episode_metadata,
            instruction_set=instruction_set,
        )
        self._store_episode(episode)
        return AddEpisodeResult(episode_uuid=episode.uuid, queued_for_dreaming=True)

    async def add_episode_bulk(self, episodes: list[Episode]) -> list[AddEpisodeResult]:
        results: list[AddEpisodeResult] = []
        for episode in episodes:
            self._check_scope_not_read_only(episode.scope)
            self.config.instruction_set(episode.instruction_set)
            self._store_episode(episode)
            results.append(AddEpisodeResult(episode_uuid=episode.uuid, queued_for_dreaming=True))
        return results

    async def add_session(
        self,
        *,
        name: str,
        turns: list[ConversationTurn],
        scope: MemoryScope,
        session_id: str | None = None,
        turns_per_episode: int = 8,
        max_chars_per_episode: int = 4000,
        time_gap_seconds: int | None = 300,
        source_description: str = "conversation session",
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
        motive: str | None = None,
    ) -> AddSessionResult:
        """Ingest a complete conversation transcript, grouping turns into episodic windows.

        Each window becomes one episode queued for dreaming. Windows are split when
        turns_per_episode, max_chars_per_episode, or time_gap_seconds is exceeded.

        Parameters
        ----------
        motive:
            WS-3 per-session Motive hint.  When set, stamps ``metadata["motive"]``
            on every episode window so the formation job can select the named Motive.
        """
        self._check_scope_not_read_only(scope)
        from memotron.session import SessionIngester

        session_metadata = dict(metadata or {})
        if motive is not None:
            session_metadata["motive"] = motive
        ingester = SessionIngester(
            client=self,
            name=name,
            scope=scope,
            session_id=session_id,
            turns_per_episode=turns_per_episode,
            max_chars_per_episode=max_chars_per_episode,
            time_gap_seconds=time_gap_seconds,
            source_description=source_description,
            metadata=session_metadata or None,
            instruction_set=instruction_set,
        )
        for turn in turns:
            await ingester.add_turn(turn.role, turn.content, timestamp=turn.timestamp, metadata=dict(turn.metadata))
        return await ingester.close()
