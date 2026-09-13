"""WS-26: The derivation DAG — runs, epochs, and re-dream.

Two DAGs, kept structurally separate (NEXT.md §2):

* the **content graph** (entities + relations, cyclic, time as attributes) —
  untouched by this module;
* the **derivation graph** (episode -> run -> relationship version, append-
  only) — a *run* (a receipted ``ReceiptRun``/``dream_job_runs`` row) is now
  addressable from every version it produced (WS-26 T1, landed in
  ``dreaming.py``/``storage/sqlite.py``), and an **epoch** is a branchable,
  HEAD-pointered generation of that graph (T2).

Re-dream is: **branch -> recompute from immutable episodes -> diff ->
adopt/rollback**, reusing WS-11 byte replay for verification (T6).

Isolation strategy
-------------------
A branch's recompute runs against a **physically separate shadow
``SQLiteStorageBackend``**, not against the live store filtered in place.
This is the one deliberate simplification this module makes relative to a
maximal in-place copy-on-write graph, and it buys three things outright
rather than through a much larger amount of code threaded across
``dreaming.py``'s dozens of scope-keyed reads:

* **HEAD is provably untouched during recompute** — the live store's
  connection is never opened for writing until :func:`adopt_epoch` merges
  the shadow's result in, so "HEAD unchanged while a branch computes" is a
  fact about which database a write landed in, not an invariant that has to
  be maintained across every reinforce/supersede/dedup lookup in the
  formation pipeline.
* **T9's registry isolation is free** — ``entity_canon``/``predicate_canon``
  overlays written during a branch's recompute live in the shadow's own
  tables; HEAD's tables are never opened for writing until adopt, so "an
  alias registered during a branched re-dream is invisible to HEAD" holds
  by construction, not by a query-time filter that has to be proven correct
  on every read path.
* **Rollback is exact** — :func:`adopt_epoch` snapshots exactly the fields
  :meth:`~memotron.storage.sqlite.SQLiteStorageBackend.graph_state_hash`
  and :meth:`~memotron.storage.sqlite.SQLiteStorageBackend.registry_state_digest`
  read before retiring/overlaying anything, so :func:`rollback_epoch` restores
  both digests byte-for-byte without needing to keep recomputing anything.

The shadow is seeded with a **baseline**: every one of HEAD's current
relationships whose ``episode_uuids`` are disjoint from the selector's
episode set is copied in verbatim (same truth key, same content, a fresh
uuid) so the recompute's reinforce/supersede/dedup decisions have the same
context a live re-ingest would — a re-dream of "last Tuesday's session"
still sees the rest of the scope's truth. The selected episodes' own
candidates (or, for Tier C, the raw episode bodies) are then (re)processed
into that seeded graph. Diffing the seeded-baseline-plus-recompute shadow
against HEAD's current state is exactly the derivation-DAG "what would
change" question T5 answers.

One epoch's shadow store is retained as a real on-disk SQLite file (never
``:memory:``, even when the main store is) — "no destruction; prior epochs
retained" applies to a branch's own audit trail too, and it is what lets
:func:`adopt_epoch` re-run WS-11 byte replay against a self-contained
ledger+graph at adopt time, independent of whatever the live store has done
since.

Non-goal reaffirmed (NEXT.md §9): nothing here creates a ``Date``/``Session``
content-plane node. :class:`EpisodeSelector` and :func:`build_session_digest`
are the derived selector/rollup surface T7 asks for; the episodes themselves
already carry ``reference_time``/``session_id`` as attributes.

Scope limitation: a crypto-shred (content-protected) scope cannot be
branched in this revision — the shadow store has its own, unrelated
``KeyManager``, so an episode body or fact sealed under the live scope's DEK
cannot be unsealed inside the shadow. :func:`branch_and_recompute` fails
fast (``ValueError``) rather than silently mishandling sealed content.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, model_validator

from memotron.config import (
    ActionabilityPolicy,
    DedupPolicy,
    DreamAgentConfig,
    DreamConfig,
    DreamJob,
    DreamJobKind,
    EntityResolutionPolicy,
    GovernancePolicy,
    MemoryBank,
    MemoryHealthPolicy,
    Motive,
    SupersessionPolicy,
)
from memotron.dreaming import DreamEngine
from memotron.extraction import InstructionalExtractor
from memotron.models import (
    Episode,
    GraphRelationship,
    MemoryScope,
    MemoryType,
    QuarantineStatus,
    RelationshipCardinality,
    RelationshipStatus,
)
from memotron.replay import byte_replay
from memotron.storage import SQLiteStorageBackend, StorageBackend

__all__ = [
    "AdoptResult",
    "EpisodeSelector",
    "EpochDiff",
    "RedreamOverrides",
    "RedreamResult",
    "RedreamTier",
    "RelationshipDiffEntry",
    "RollbackResult",
    "adopt_epoch",
    "branch_and_recompute",
    "build_session_digest",
    "diff_epochs",
    "epoch_content_digest",
    "open_shadow_store",
    "rollback_epoch",
    "select_episodes",
    "select_tier",
]


# ---------------------------------------------------------------------------
# T8: re-dream cost tiers
# ---------------------------------------------------------------------------


class RedreamTier(StrEnum):
    """WS-26 T8: the cheapest recompute strategy sufficient for a given override.

    Never silently upgraded past what :func:`select_tier` names — the run
    that recomputes an epoch always records which tier it ran at and why.
    """

    A = "A"
    """Governance-only: ``DreamEngine.regovern_scope`` over stored candidates
    that were never PROMOTED or were QUARANTINED. Zero LLM/extraction calls."""

    B = "B"
    """Resolution/supersession/dedup: the same zero-extraction-call regovern
    path, but every stored candidate for the selection is re-litigated
    (forced back to PENDING first) regardless of its original disposition —
    a resolution-policy change can affect an already-PROMOTED fact."""

    C = "C"
    """Full re-extraction: the selected episodes' raw bodies are re-run
    through the extraction transport (a model/prompt/instruction-set change
    cannot be satisfied from stored candidates alone)."""


@dataclass(frozen=True)
class RedreamOverrides:
    """WS-26 T8/T4: the override set a branch recomputes under.

    Every field is optional; the empty default recomputes at Tier A under the
    scope's current config exactly as it stands (a pure "re-govern this
    epoch" branch). ``extraction_transport``/``instruction_set`` force Tier C;
    ``dedup``/``entity_resolution``/``supersession`` (or a motive that sets
    ``dedup_threshold``) select Tier B; anything else stays Tier A.
    """

    governance: GovernancePolicy | None = None
    actionability: ActionabilityPolicy | None = None
    health: MemoryHealthPolicy | None = None
    dedup: DedupPolicy | None = None
    entity_resolution: EntityResolutionPolicy | None = None
    supersession: SupersessionPolicy | None = None
    memory_bank: MemoryBank | None = None
    """Replaces ``DreamConfig.memory_bank`` wholesale for the recompute — the
    mechanism for changing a Motive's own definition (e.g. its
    ``allowed_memory_types``) rather than which Motive is selected."""
    motive: str | None = None
    """Which Motive governs the recompute job. For Tier A/B this only takes
    effect for candidates that did not already carry their own
    ``motive_name`` (stored per-candidate at extraction time); Tier C applies
    it to every selected episode via the job pin."""
    instruction_set: str | None = None
    extraction_transport: Any = None
    """An already-constructed ``ExtractionTransport``. Setting this forces
    Tier C and is the only way to recompute under a different model."""
    label: str = ""

    def as_receipt_dict(self) -> dict[str, Any]:
        """JSON-safe summary recorded on the epoch's bookkeeping row."""
        return {
            "governance": self.governance.model_dump(mode="json") if self.governance is not None else None,
            "actionability": self.actionability.model_dump(mode="json") if self.actionability is not None else None,
            "health": self.health.model_dump(mode="json") if self.health is not None else None,
            "dedup": self.dedup.model_dump(mode="json") if self.dedup is not None else None,
            "entity_resolution": (
                self.entity_resolution.model_dump(mode="json") if self.entity_resolution is not None else None
            ),
            "supersession": self.supersession.model_dump(mode="json") if self.supersession is not None else None,
            "memory_bank": self.memory_bank.model_dump(mode="json") if self.memory_bank is not None else None,
            "motive": self.motive,
            "instruction_set": self.instruction_set,
            "extraction_transport": (
                type(self.extraction_transport).__name__ if self.extraction_transport is not None else None
            ),
            "label": self.label,
        }


def select_tier(overrides: RedreamOverrides, *, resolved_motive: Motive | None = None) -> tuple[RedreamTier, str]:
    """WS-26 T8: pure function — the override set -> the cheapest sufficient tier + why.

    ``resolved_motive`` is the ACTUAL ``Motive`` object ``overrides.motive``
    names (resolved by the caller against ``overrides.memory_bank`` or the
    base config's bank) — passed in rather than re-resolved here so this stays
    a pure function with no config/bank lookup of its own.
    """
    if overrides.extraction_transport is not None:
        return RedreamTier.C, "extraction_transport override forces full re-extraction"
    if overrides.instruction_set is not None:
        return RedreamTier.C, "instruction_set override forces full re-extraction"
    if resolved_motive is not None and (
        resolved_motive.prompt_profile is not None
        or resolved_motive.prompt_override is not None
        or resolved_motive.system_prompt_override is not None
    ):
        return (
            RedreamTier.C,
            f"motive {resolved_motive.name!r} changes the extraction prompt (prompt_profile/"
            "prompt_override/system_prompt_override)",
        )
    if overrides.dedup is not None or overrides.entity_resolution is not None or overrides.supersession is not None:
        return RedreamTier.B, "resolution/supersession/dedup override — re-resolve without re-extracting"
    if resolved_motive is not None and resolved_motive.dedup_threshold is not None:
        return RedreamTier.B, f"motive {resolved_motive.name!r} sets a dedup_threshold override"
    return RedreamTier.A, "governance-only (or no) override — re-govern stored candidates"


# ---------------------------------------------------------------------------
# T3: episode selection
# ---------------------------------------------------------------------------


class EpisodeSelector(BaseModel):
    """WS-26 T3: the immutable-episode-set selector for a re-dream.

    Exactly one of the four fields must be set. ``date_range`` is a half-open
    ``[start, end)`` interval over ``Episode.reference_time``.
    """

    model_config = {"frozen": True}

    session_id: str | None = None
    date_range: tuple[datetime, datetime] | None = None
    entity_uuid: str | None = None
    episode_uuids: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> EpisodeSelector:
        set_count = sum(
            value is not None for value in (self.session_id, self.date_range, self.entity_uuid, self.episode_uuids)
        )
        if set_count != 1:
            raise ValueError("EpisodeSelector requires exactly one of session_id/date_range/entity_uuid/episode_uuids")
        return self


def select_episodes(*, storage: StorageBackend, scope: MemoryScope, selector: EpisodeSelector) -> tuple[Episode, ...]:
    """WS-26 T3: resolve *selector* to its immutable episode set for one scope.

    Deterministic order: ``(reference_time, uuid)``, so a re-dream over the
    same selector always processes episodes in the same order.
    """
    episodes = storage.episodes_for_scope(scope.key)
    if selector.episode_uuids is not None:
        wanted = set(selector.episode_uuids)
        matched = [episode for episode in episodes if episode.uuid in wanted]
    elif selector.session_id is not None:
        matched = [episode for episode in episodes if episode.metadata.get("session_id") == selector.session_id]
    elif selector.date_range is not None:
        start, end = selector.date_range
        matched = [episode for episode in episodes if start <= episode.reference_time < end]
    else:
        assert selector.entity_uuid is not None
        mentioning_episode_node_uuids = {
            relationship.source_uuid
            for relationship in storage.relationships()
            if relationship.type == "MENTIONS"
            and relationship.target_uuid == selector.entity_uuid
            and relationship.properties.get("scope_key") == scope.key
        }
        mentioning_episode_uuids: set[str] = set()
        for node_uuid in mentioning_episode_node_uuids:
            try:
                node = storage.get_node(node_uuid)
            except ValueError:
                continue
            episode_uuid = node.properties.get("episode_uuid")
            if episode_uuid:
                mentioning_episode_uuids.add(str(episode_uuid))
        matched = [episode for episode in episodes if episode.uuid in mentioning_episode_uuids]
    return tuple(sorted(matched, key=lambda episode: (episode.reference_time, episode.uuid)))


# ---------------------------------------------------------------------------
# Shadow-store plumbing
# ---------------------------------------------------------------------------


def _shadow_store_path(main_storage: StorageBackend, *, shadow_root: Path | str | None) -> Path:
    """A fresh, real (never ``:memory:``) on-disk path for one epoch's shadow store."""
    if shadow_root is not None:
        root = Path(shadow_root)
    else:
        db_path = getattr(main_storage, "db_path", None)
        if isinstance(db_path, Path) and str(db_path) != ":memory:":
            root = db_path.parent / ".memotron-epochs"
        else:
            root = Path(tempfile.mkdtemp(prefix="memotron-epochs-"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"shadow-{secrets.token_hex(16)}.sqlite"


def _copy_relationship(
    *, source_store: StorageBackend, dest_store: StorageBackend, relationship: GraphRelationship
) -> GraphRelationship:
    """Copy one relationship (and its two endpoint nodes) into *dest_store*.

    The relationship gets a fresh uuid in *dest_store*; its endpoint nodes
    converge on the SAME node if one with that ``graph_key`` already exists
    there (``upsert_node`` is keyed by the normalised key), exactly like a
    live formation write would.
    """
    source_node = source_store.get_node(relationship.source_uuid)
    target_node = source_store.get_node(relationship.target_uuid)
    new_source, _ = dest_store.upsert_node(
        labels=source_node.labels,
        key=str(source_node.properties.get("graph_key") or source_node.uuid),
        properties=dict(source_node.properties),
        valid_from=source_node.valid_from,
        valid_to=source_node.valid_to,
    )
    new_target, _ = dest_store.upsert_node(
        labels=target_node.labels,
        key=str(target_node.properties.get("graph_key") or target_node.uuid),
        properties=dict(target_node.properties),
        valid_from=target_node.valid_from,
        valid_to=target_node.valid_to,
    )
    return dest_store.add_relationship(
        source_uuid=new_source.uuid,
        target_uuid=new_target.uuid,
        relationship_type=relationship.type,
        properties=dict(relationship.properties),
        valid_from=relationship.valid_from,
        valid_to=relationship.valid_to,
    )


def _seed_shadow_baseline(
    *, main_storage: StorageBackend, shadow: StorageBackend, scope: MemoryScope, excluded_episode_uuids: frozenset[str]
) -> int:
    """Copy every HEAD relationship whose ``episode_uuids`` are disjoint from
    *excluded_episode_uuids* into *shadow*.

    This is the context the recompute needs for correct reinforce/supersede/
    dedup decisions over whatever it DOES (re)materialize — a branch of "last
    Tuesday" still sees the rest of the scope's truth, exactly as a live
    re-ingest would.

    *excluded_episode_uuids* is tier-dependent, not simply "the selection":
    Tier B/C fully reprocess the selected episodes from stored candidates or
    raw bodies, so their relationships are excluded here and expected to be
    reproduced (or superseded) by the recompute.  Tier A's
    ``regovern_scope`` explicitly leaves already-PROMOTED candidates
    untouched (only PENDING/QUARANTINED rows are re-litigated) — a Tier A
    branch must therefore seed EVERYTHING, including the selection's already-
    promoted facts (the caller passes an empty exclusion set), or those facts
    would silently vanish from the shadow purely because governance never
    reconsidered candidates that were already accepted.
    """
    seeded = 0
    for relationship in main_storage.relationships_for_scope(scope.key):
        episode_uuids = set(relationship.properties.get("episode_uuids") or [])
        if episode_uuids & excluded_episode_uuids:
            continue
        _copy_relationship(source_store=main_storage, dest_store=shadow, relationship=relationship)
        seeded += 1
    return seeded


def _seed_shadow_candidates(
    *,
    main_storage: StorageBackend,
    shadow: StorageBackend,
    scope: MemoryScope,
    selected_episode_uuids: frozenset[str],
    force_pending: bool,
) -> int:
    """WS-26 T8 (Tier A/B): copy the selection's stored stage-1 candidates.

    Tier A (``force_pending=False``) preserves each candidate's original
    disposition, so the shadow's ``regovern_scope`` call only re-litigates
    rows that were never PROMOTED — governance re-applies, truth that already
    stood is not re-litigated, matching what live ``regovern_scope`` does.

    Tier B (``force_pending=True``) resets every non-DISCARDED candidate back
    to PENDING, so ``regovern_scope`` re-litigates PROMOTED rows too — a
    resolution/dedup/supersession policy change can affect an already-
    accepted fact. DISCARDED stays DISCARDED: that is an operator's
    permanent decision, not something a policy re-dream may resurrect.
    """
    seeded = 0
    for candidate in main_storage.quarantined_candidates(scope_key=scope.key, status=None, limit=None):
        if candidate.episode_uuid not in selected_episode_uuids:
            continue
        payload_text = main_storage.quarantined_candidate_payload(candidate.candidate_uuid)
        status = candidate.status
        if force_pending and status != QuarantineStatus.DISCARDED:
            status = QuarantineStatus.PENDING
        shadow.quarantine_candidate(
            candidate_uuid=candidate.candidate_uuid,
            scope_key=scope.key,
            episode_uuid=candidate.episode_uuid,
            reason=candidate.reason,
            detail=candidate.detail,
            saves_step=candidate.saves_step,
            subject=candidate.subject,
            predicate=candidate.predicate,
            object_text=candidate.object,
            proposed_relationship_type=candidate.proposed_relationship_type,
            proposed_memory_type=candidate.proposed_memory_type,
            candidate_payload=payload_text,
            candidate_digest=candidate.candidate_digest,
            instruction_set=candidate.instruction_set,
            motive_name=candidate.motive_name,
            quarantined_at=candidate.quarantined_at,
            status=status,
        )
        seeded += 1
    return seeded


def _group_episodes_by_instruction_set(episodes: tuple[Episode, ...]) -> list[tuple[str, list[Episode]]]:
    groups: dict[str, list[Episode]] = {}
    order: list[str] = []
    for episode in episodes:
        if episode.instruction_set not in groups:
            groups[episode.instruction_set] = []
            order.append(episode.instruction_set)
        groups[episode.instruction_set].append(episode)
    return [(name, groups[name]) for name in order]


def _apply_overrides_to_config(config: DreamConfig, overrides: RedreamOverrides) -> DreamConfig:
    updates: dict[str, Any] = {}
    if overrides.governance is not None:
        updates["governance"] = overrides.governance
    if overrides.actionability is not None:
        updates["actionability"] = overrides.actionability
    if overrides.health is not None:
        updates["health"] = overrides.health
    if overrides.dedup is not None:
        updates["dedup"] = overrides.dedup
    if overrides.entity_resolution is not None:
        updates["entity_resolution"] = overrides.entity_resolution
    if overrides.supersession is not None:
        updates["supersession"] = overrides.supersession
    if overrides.memory_bank is not None:
        updates["memory_bank"] = overrides.memory_bank
    if not updates:
        return config
    return config.model_copy(update=updates)


# ---------------------------------------------------------------------------
# T4: branch + recompute
# ---------------------------------------------------------------------------


class RedreamResult(BaseModel):
    """WS-26 T4/T8: the outcome of one ``branch_and_recompute`` call."""

    model_config = {"frozen": True}

    epoch_id: str
    parent_epoch_id: str
    tier: RedreamTier
    tier_reason: str
    episodes_selected: int
    baseline_relationships_seeded: int
    created_relationships: int
    reinforced_relationships: int
    superseded_relationships: int
    run_uuids: tuple[str, ...]
    shadow_store_path: str


async def branch_and_recompute(
    *,
    main_storage: StorageBackend,
    config: DreamConfig,
    scope: MemoryScope,
    selector: EpisodeSelector,
    extractor: InstructionalExtractor,
    embedding_transport: Any = None,
    agent_transport: Any = None,
    rollup_synthesis_transport: Any = None,
    overrides: RedreamOverrides | None = None,
    now: datetime | None = None,
    shadow_root: Path | str | None = None,
) -> RedreamResult:
    """WS-26 T3/T4/T8/T9: fork a new epoch and recompute it over *selector*'s
    episodes at the cheapest sufficient tier — HEAD is never opened for
    writing until :func:`adopt_epoch`.

    ``extractor``/``embedding_transport``/``agent_transport``/
    ``rollup_synthesis_transport`` are the SAME objects a live
    ``Memotron`` client would pass to its own ``DreamEngine`` — this
    function builds a second, throwaway engine bound to the shadow store
    rather than reusing the caller's, so it never touches the live store's
    connection.
    """
    overrides = overrides or RedreamOverrides()
    now = now or datetime.now(UTC)
    if main_storage.scope_content_is_protected(scope.key):
        raise ValueError(
            f"scope {scope.key!r} is crypto-shred content-protected; branched re-dream "
            "is not supported for a protected scope in this revision (WS-26 scope limitation "
            "— the shadow store has its own, unrelated KeyManager)"
        )
    base_epoch_id = main_storage.ensure_root_epoch(scope.key, now=now)
    selected_episodes = select_episodes(storage=main_storage, scope=scope, selector=selector)
    if not selected_episodes:
        raise ValueError(f"selector matched zero episodes for scope {scope.key!r}: {selector!r}")
    selected_uuids = frozenset(episode.uuid for episode in selected_episodes)

    bank = overrides.memory_bank or config.memory_bank
    resolved_motive: Motive | None = None
    if overrides.motive is not None:
        if bank is None:
            raise ValueError(f"motive override {overrides.motive!r} requires a MemoryBank")
        resolved_motive = bank.motive(overrides.motive)
    tier, tier_reason = select_tier(overrides, resolved_motive=resolved_motive)

    shadow_path = _shadow_store_path(main_storage, shadow_root=shadow_root)
    epoch_id = main_storage.create_epoch(
        scope_key=scope.key,
        parent_epoch_id=base_epoch_id,
        now=now,
        label=overrides.label,
        status="open",
        tier=tier.value,
        overrides=overrides.as_receipt_dict(),
        shadow_store_path=str(shadow_path),
    )
    shadow = SQLiteStorageBackend(shadow_path)
    try:
        # Tier A's regovern_scope leaves already-PROMOTED candidates
        # untouched by design, so a Tier A branch must seed the WHOLE scope
        # (nothing excluded) — otherwise an already-accepted fact from the
        # selection would simply be absent from the shadow.  Tier B/C fully
        # reprocess the selection, so its relationships are excluded here and
        # left for the recompute to reproduce or supersede.
        baseline_exclusions = frozenset() if tier is RedreamTier.A else selected_uuids
        baseline_seeded = _seed_shadow_baseline(
            main_storage=main_storage, shadow=shadow, scope=scope, excluded_episode_uuids=baseline_exclusions
        )
        for episode in selected_episodes:
            shadow.add_episode(episode)

        shadow_config = _apply_overrides_to_config(config, overrides)
        run_uuids: list[str] = []
        created = reinforced = superseded = 0

        if tier is RedreamTier.C:
            shadow_extractor = (
                InstructionalExtractor(transport=overrides.extraction_transport)
                if overrides.extraction_transport is not None
                else extractor
            )
            shadow_engine = DreamEngine(
                config=shadow_config,
                graph=shadow,
                extractor=shadow_extractor,
                agent_transport=agent_transport,
                embedding_transport=embedding_transport,
                rollup_synthesis_transport=rollup_synthesis_transport,
                epoch_override=epoch_id,
                redream_tier=tier.value,
                redream_tier_reason=tier_reason,
            )
            for instruction_set_name, group in _group_episodes_by_instruction_set(selected_episodes):
                job = DreamJob(
                    name=f"epoch-recompute-{epoch_id}",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    scope=scope,
                    agent=DreamAgentConfig(agent_id="epoch-recompute", name="Epoch Recompute"),
                    instruction_set=overrides.instruction_set or instruction_set_name,
                    motive=overrides.motive,
                )
                job_run = await shadow_engine.run_job(job=job, episodes=list(group), now=now)
                if job_run.run_uuid is not None:
                    shadow.record_epoch_run(epoch_id, job_run.run_uuid, now=now)
                    run_uuids.append(job_run.run_uuid)
                created += job_run.created_relationships
                reinforced += job_run.reinforced_relationships
                superseded += job_run.superseded_relationships
        else:
            # Tier A / Tier B: zero extraction/LLM calls by construction —
            # ``regovern_scope``/``regovern_candidate`` never call the
            # extraction transport, so the extractor wired into this engine
            # is inert for this path (kept so a shared engine could serve
            # both purposes; a caller wanting a hard proof passes a
            # call-counting spy transport here and asserts it is never hit).
            shadow_engine = DreamEngine(
                config=shadow_config,
                graph=shadow,
                extractor=extractor,
                agent_transport=agent_transport,
                embedding_transport=embedding_transport,
                rollup_synthesis_transport=rollup_synthesis_transport,
                epoch_override=epoch_id,
                redream_tier=tier.value,
                redream_tier_reason=tier_reason,
            )
            _seed_shadow_candidates(
                main_storage=main_storage,
                shadow=shadow,
                scope=scope,
                selected_episode_uuids=selected_uuids,
                force_pending=(tier is RedreamTier.B),
            )
            result = await shadow_engine.regovern_scope(scope=scope, now=now, resolved_by=f"epoch:{epoch_id}")
            if result.run_uuid is not None:
                shadow.record_epoch_run(epoch_id, result.run_uuid, now=now)
                run_uuids.append(result.run_uuid)
            created = result.promoted

        main_storage.set_epoch_status(epoch_id, "ready")
    except Exception:
        main_storage.set_epoch_status(epoch_id, "discarded")
        raise
    finally:
        shadow.close()

    return RedreamResult(
        epoch_id=epoch_id,
        parent_epoch_id=base_epoch_id,
        tier=tier,
        tier_reason=tier_reason,
        episodes_selected=len(selected_episodes),
        baseline_relationships_seeded=baseline_seeded,
        created_relationships=created,
        reinforced_relationships=reinforced,
        superseded_relationships=superseded,
        run_uuids=tuple(run_uuids),
        shadow_store_path=str(shadow_path),
    )


# ---------------------------------------------------------------------------
# T5: diff two epochs
# ---------------------------------------------------------------------------


class RelationshipDiffEntry(BaseModel):
    model_config = {"frozen": True}

    uuid: str
    truth_key: str | None
    relationship_type: str
    fact: str | None
    status: str | None
    produced_by_run: str | None


class EpochDiff(BaseModel):
    """WS-26 T5: ``{added, removed, changed(by truth_slot_key), unchanged}``
    plus the T9 registry deltas."""

    model_config = {"frozen": True}

    added: tuple[RelationshipDiffEntry, ...]
    removed: tuple[RelationshipDiffEntry, ...]
    changed: tuple[tuple[RelationshipDiffEntry, RelationshipDiffEntry], ...]
    unchanged: tuple[RelationshipDiffEntry, ...]
    aliases_added: tuple[str, ...]
    aliases_changed: tuple[str, ...]
    aliases_removed: tuple[str, ...]
    canonicals_added: tuple[str, ...]
    canonicals_changed: tuple[str, ...]
    canonicals_removed: tuple[str, ...]


def _diff_entry(relationship: GraphRelationship) -> RelationshipDiffEntry:
    props = relationship.properties
    return RelationshipDiffEntry(
        uuid=relationship.uuid,
        truth_key=props.get("truth_key"),
        relationship_type=relationship.type,
        fact=props.get("fact"),
        status=props.get("status"),
        produced_by_run=props.get("produced_by_run"),
    )


def _relationship_changed(old: GraphRelationship, new: GraphRelationship) -> bool:
    return (old.properties.get("fact"), old.properties.get("confidence"), old.properties.get("status")) != (
        new.properties.get("fact"),
        new.properties.get("confidence"),
        new.properties.get("status"),
    )


def _registry_delta(
    *, before: list[dict[str, Any]], after: list[dict[str, Any]], key: str, value_fields: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    before_by_key = {row[key]: row for row in before}
    after_by_key = {row[key]: row for row in after}
    added = tuple(sorted(after_by_key.keys() - before_by_key.keys()))
    removed = tuple(sorted(before_by_key.keys() - after_by_key.keys()))
    changed = tuple(
        sorted(
            name
            for name in (after_by_key.keys() & before_by_key.keys())
            if tuple(before_by_key[name].get(field_) for field_ in value_fields)
            != tuple(after_by_key[name].get(field_) for field_ in value_fields)
        )
    )
    return added, changed, removed


def epoch_content_digest(storage: StorageBackend, scope: MemoryScope) -> str:
    """WS-26 T8: a uuid-independent content digest of one store's active facts.

    ``graph_state_hash`` deliberately includes each row's literal uuid (it
    exists to detect ANY change to a specific already-identified row across a
    mutation, which is exactly what byte replay needs) — so two
    INDEPENDENTLY recomputed shadows can never be ``graph_state_hash``-equal
    even when they materialize identical facts, because each fresh
    materialization mints its own random uuid.  This digest instead hashes
    the sorted ``(truth_key, fact, confidence, status, memory_type)`` tuples,
    which is the right equality notion for "did Tier B recompute the same
    epoch Tier C would have" (T8's integration test): content-identical,
    lineage/identity-independent.
    """
    rows = sorted(
        (
            relationship.properties.get("truth_key") or relationship.uuid,
            relationship.properties.get("fact"),
            relationship.properties.get("confidence"),
            relationship.properties.get("status"),
            relationship.properties.get("memory_type"),
        )
        for relationship in storage.relationships_for_scope(scope.key)
        if relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
    )
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def diff_epochs(*, main_storage: StorageBackend, shadow_storage: StorageBackend, scope: MemoryScope) -> EpochDiff:
    """WS-26 T5: diff HEAD's current epoch against a recomputed shadow epoch.

    Grouped by ``truth_key`` (falling back to uuid for a row with none, e.g.
    a MULTI_ACTIVE row that predates truth-key stamping) — a changed fact
    shows in ``changed``, never as one ``added`` plus one ``removed``.
    """
    cur_rows = [
        r
        for r in main_storage.relationships_for_scope(scope.key)
        if r.properties.get("status") == RelationshipStatus.ACTIVE.value
    ]
    new_rows = [
        r
        for r in shadow_storage.relationships_for_scope(scope.key)
        if r.properties.get("status") == RelationshipStatus.ACTIVE.value
    ]
    cur_by_key = {(r.properties.get("truth_key") or r.uuid): r for r in cur_rows}
    new_by_key = {(r.properties.get("truth_key") or r.uuid): r for r in new_rows}

    added_keys = new_by_key.keys() - cur_by_key.keys()
    removed_keys = cur_by_key.keys() - new_by_key.keys()
    common_keys = cur_by_key.keys() & new_by_key.keys()

    changed: list[tuple[RelationshipDiffEntry, RelationshipDiffEntry]] = []
    unchanged: list[RelationshipDiffEntry] = []
    for key in sorted(common_keys):
        old, new = cur_by_key[key], new_by_key[key]
        if _relationship_changed(old, new):
            changed.append((_diff_entry(old), _diff_entry(new)))
        else:
            unchanged.append(_diff_entry(new))

    entity_added, entity_changed, entity_removed = _registry_delta(
        before=main_storage.entity_alias_rows_for_scope(scope.key),
        after=shadow_storage.entity_alias_rows_for_scope(scope.key),
        key="name_normalized",
        value_fields=("canonical_name", "status"),
    )
    predicate_added, predicate_changed, predicate_removed = _registry_delta(
        before=main_storage.predicate_alias_rows_for_scope(scope.key),
        after=shadow_storage.predicate_alias_rows_for_scope(scope.key),
        key="predicate_normalized",
        value_fields=("canonical_predicate",),
    )

    return EpochDiff(
        added=tuple(_diff_entry(new_by_key[key]) for key in sorted(added_keys)),
        removed=tuple(_diff_entry(cur_by_key[key]) for key in sorted(removed_keys)),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        aliases_added=entity_added,
        aliases_changed=entity_changed,
        aliases_removed=entity_removed,
        canonicals_added=predicate_added,
        canonicals_changed=predicate_changed,
        canonicals_removed=predicate_removed,
    )


# ---------------------------------------------------------------------------
# T6: adopt / rollback + replay verify
# ---------------------------------------------------------------------------


class AdoptResult(BaseModel):
    model_config = {"frozen": True}

    epoch_id: str
    previous_epoch_id: str | None
    diff: EpochDiff


class RollbackResult(BaseModel):
    model_config = {"frozen": True}

    rolled_back_from: str
    restored_to: str


def open_shadow_store(main_storage: StorageBackend, epoch_id: str) -> SQLiteStorageBackend:
    """Open the shadow store a branch's recompute wrote into.

    The one place outside ``storage/`` that constructs a
    ``SQLiteStorageBackend`` directly — callers (client.py's
    ``redream_diff``, this module's own :func:`adopt_epoch`) go through this
    instead of reaching for the class themselves, so "which epoch's shadow
    is this" stays a single lookup. The caller is responsible for closing it.
    """
    epoch_row = main_storage.epoch(epoch_id)
    shadow_path = epoch_row["shadow_store_path"]
    if not shadow_path:
        raise ValueError(f"epoch {epoch_id} has no recompute (no shadow store recorded)")
    return SQLiteStorageBackend(shadow_path)


def _relationship_snapshot(relationship: GraphRelationship) -> dict[str, Any]:
    """The exact fields :meth:`graph_state_hash` reads, captured before a retire."""
    props = relationship.properties
    return {
        "uuid": relationship.uuid,
        "status": props.get("status"),
        "valid_to": relationship.valid_to.isoformat() if relationship.valid_to else None,
        "superseded_by_relationship_uuid": props.get("superseded_by_relationship_uuid"),
        "superseded_by_run": props.get("superseded_by_run"),
        "active_in_context": props.get("active_in_context", True),
    }


async def adopt_epoch(
    *, main_storage: StorageBackend, scope: MemoryScope, epoch_id: str, now: datetime | None = None
) -> AdoptResult:
    """WS-26 T6: verify, then adopt — a pointer flip that also merges exactly
    the rows the diff says changed.

    Order (fail-closed): (1) byte-replay every run recorded against
    *epoch_id*, against the shadow's OWN self-contained ledger+graph — a
    :class:`~memotron.replay.ReplayVerificationError` aborts here, before
    a single row in the live store is touched; (2) diff against HEAD;
    (3) inside one :meth:`~memotron.storage.base.OperationalStorage.transaction`
    bracket, merge the shadow's added/changed rows and registry overlays into
    the live store (fresh uuids, tagged with the epoch that produced them —
    already ``epoch_id`` on every merged row, stamped when the shadow
    recomputed it), retire the rows they replace (recording an exact
    before-snapshot for :func:`rollback_epoch`), then flip
    ``active_epochs``. HEAD's rows that the selection never touched are never
    written to at all.
    """
    now = now or datetime.now(UTC)
    epoch_row = main_storage.epoch(epoch_id)
    if epoch_row["scope_key"] != scope.key:
        raise ValueError(f"epoch {epoch_id} belongs to scope {epoch_row['scope_key']!r}, not {scope.key!r}")
    if epoch_row["status"] not in ("open", "ready"):
        raise ValueError(f"epoch {epoch_id} is not adoptable from status {epoch_row['status']!r}")

    shadow = open_shadow_store(main_storage, epoch_id)
    try:
        for run_uuid in shadow.epoch_run_uuids(epoch_id):
            # Raises ReplayVerificationError on any divergence — propagates
            # uncaught, and the pointer has not moved.
            await byte_replay(shadow.receipts, run_uuid=run_uuid, graph=shadow)

        diff = diff_epochs(main_storage=main_storage, shadow_storage=shadow, scope=scope)
        parent_epoch_id = epoch_row["parent_epoch_id"]

        with main_storage.transaction():
            snapshot: dict[str, Any] = {"relationships": [], "entity_canon": [], "predicate_canon": []}
            new_uuid_by_truth_key: dict[str, str] = {}

            for entry in list(diff.added) + [pair[1] for pair in diff.changed]:
                shadow_relationship = shadow.get_relationship(entry.uuid)
                merged = _copy_relationship(
                    source_store=shadow, dest_store=main_storage, relationship=shadow_relationship
                )
                key = shadow_relationship.properties.get("truth_key") or entry.uuid
                new_uuid_by_truth_key[key] = merged.uuid

            for entry in list(diff.removed) + [pair[0] for pair in diff.changed]:
                old = main_storage.get_relationship(entry.uuid)
                snapshot["relationships"].append(_relationship_snapshot(old))
                key = old.properties.get("truth_key") or entry.uuid
                successor_uuid = new_uuid_by_truth_key.get(key)
                retire_properties: dict[str, Any] = {
                    "superseded_at": now.isoformat(),
                    "epoch_adopt_retired_by_epoch": epoch_id,
                }
                if successor_uuid is not None:
                    successor = main_storage.get_relationship(successor_uuid)
                    retire_properties["superseded_by_relationship_uuid"] = successor_uuid
                    retire_properties["superseded_by_run"] = successor.properties.get("produced_by_run")
                main_storage.mark_relationship(
                    entry.uuid, status=RelationshipStatus.SUPERSEDED, valid_to=now, properties=retire_properties
                )

            for name in list(diff.aliases_added) + list(diff.aliases_changed):
                before = main_storage.entity_alias_row(scope.key, name)
                snapshot["entity_canon"].append({"name": name, "before": before})
                row = shadow.entity_alias_row(scope.key, name)
                assert row is not None
                main_storage.overlay_entity_alias_row(scope.key, row, epoch_id=epoch_id)

            for name in list(diff.canonicals_added) + list(diff.canonicals_changed):
                before = main_storage.predicate_alias_row(scope.key, name)
                snapshot["predicate_canon"].append({"name": name, "before": before})
                row = shadow.predicate_alias_row(scope.key, name)
                assert row is not None
                main_storage.overlay_predicate_row(scope.key, row, epoch_id=epoch_id)

            main_storage.set_epoch_pre_adopt_snapshot(epoch_id, snapshot)
            main_storage.set_epoch_status(epoch_id, "adopted")
            if parent_epoch_id is not None:
                main_storage.set_epoch_status(parent_epoch_id, "superseded")
            main_storage.set_active_epoch(scope.key, epoch_id, now=now)

        return AdoptResult(epoch_id=epoch_id, previous_epoch_id=parent_epoch_id, diff=diff)
    finally:
        shadow.close()


async def rollback_epoch(
    *, main_storage: StorageBackend, scope: MemoryScope, now: datetime | None = None
) -> RollbackResult:
    """WS-26 T6: restore the scope's HEAD to its state immediately before the
    last adopt — a single-step rollback to the current active epoch's
    immediate parent, restoring the exact pre-adopt snapshot
    :func:`adopt_epoch` recorded.

    Only single-step rollback (to the immediate parent) is supported: that is
    what the recorded snapshot is exact for. Rolling back further is adopting
    an even-older epoch's own parent snapshot by calling this again, or
    branching fresh from an ancestor epoch.
    """
    now = now or datetime.now(UTC)
    current_epoch_id = main_storage.active_epoch_for_scope(scope.key)
    if current_epoch_id is None:
        raise ValueError(f"scope {scope.key!r} has no active epoch")
    current = main_storage.epoch(current_epoch_id)
    parent_epoch_id = current["parent_epoch_id"]
    if parent_epoch_id is None:
        raise ValueError(f"epoch {current_epoch_id} is the root epoch; nothing to roll back to")
    snapshot = current["pre_adopt_snapshot"]
    if snapshot is None:
        raise ValueError(
            f"epoch {current_epoch_id} has no recorded pre-adopt snapshot (it was never adopted through adopt_epoch)"
        )

    with main_storage.transaction():
        for entry in snapshot["relationships"]:
            valid_to = datetime.fromisoformat(entry["valid_to"]) if entry["valid_to"] else None
            main_storage.update_relationship(
                entry["uuid"],
                properties={
                    "status": entry["status"],
                    "superseded_by_relationship_uuid": entry["superseded_by_relationship_uuid"],
                    "superseded_by_run": entry["superseded_by_run"],
                    "active_in_context": entry["active_in_context"],
                },
                valid_to=valid_to,
                clear_valid_to=valid_to is None,
            )
        for row_entry in snapshot["entity_canon"]:
            if row_entry["before"] is None:
                main_storage.delete_entity_alias_row(scope.key, row_entry["name"])
            else:
                before_row = row_entry["before"]
                main_storage.overlay_entity_alias_row(scope.key, before_row, epoch_id=before_row.get("epoch_id", ""))
        for row_entry in snapshot["predicate_canon"]:
            if row_entry["before"] is None:
                main_storage.delete_predicate_row(scope.key, row_entry["name"])
            else:
                before_row = row_entry["before"]
                main_storage.overlay_predicate_row(scope.key, before_row, epoch_id=before_row.get("epoch_id", ""))
        main_storage.set_epoch_status(current_epoch_id, "discarded")
        main_storage.set_epoch_status(parent_epoch_id, "adopted")
        main_storage.set_active_epoch(scope.key, parent_epoch_id, now=now)

    return RollbackResult(rolled_back_from=current_epoch_id, restored_to=parent_epoch_id)


# ---------------------------------------------------------------------------
# T7: Date/Session as derived selectors — a session_digest rollup
# ---------------------------------------------------------------------------


def build_session_digest(
    *, storage: StorageBackend, scope: MemoryScope, session_id: str, now: datetime | None = None
) -> GraphRelationship | None:
    """WS-26 T7: a derived ``rollup`` summarizing one session's episodes.

    NOT a content-plane substrate node (NEXT.md §9 non-goal) — it is a
    ``memory_type=rollup`` relationship, exactly like a consolidation THEME,
    pointing at the facts it summarizes via ``derived_from``. Deterministic
    structural label, no LLM. Returns ``None`` when the session produced no
    active facts (nothing to summarize).
    """
    now = now or datetime.now(UTC)
    selector = EpisodeSelector(session_id=session_id)
    episodes = select_episodes(storage=storage, scope=scope, selector=selector)
    episode_uuids = frozenset(episode.uuid for episode in episodes)
    if not episode_uuids:
        return None
    session_facts = [
        relationship
        for relationship in storage.relationships_for_scope(scope.key)
        if relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
        and set(relationship.properties.get("episode_uuids") or []) & episode_uuids
    ]
    if not session_facts:
        return None
    fact_uuids = sorted(relationship.uuid for relationship in session_facts)
    reference_times = [episode.reference_time for episode in episodes]
    digest_text = (
        f"Session {session_id}: {len(episodes)} episode(s), {len(session_facts)} fact(s) "
        f"formed/updated between {min(reference_times).isoformat()} and {max(reference_times).isoformat()}."
    )
    subject_key = f"{scope.key}:SessionDigest:{session_id}"
    subject_node, _ = storage.upsert_node(
        labels=("SessionDigest", scope.kind.value.title()),
        key=subject_key,
        properties={
            "name": f"session:{session_id}",
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
        },
    )
    object_node, _ = storage.upsert_node(
        labels=("Entity", scope.kind.value.title()),
        key=f"{scope.key}:Entity:session-summary:{session_id}",
        properties={
            "name": digest_text,
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
        },
    )
    truth_key = f"{scope.key}:session:{session_id}:summarizes"
    active_epoch_id = storage.ensure_root_epoch(scope.key, now=now)
    return storage.add_relationship(
        source_uuid=subject_node.uuid,
        target_uuid=object_node.uuid,
        relationship_type="ROLLUP",
        properties={
            "fact": digest_text,
            "object": digest_text,
            "predicate": "summarizes",
            "confidence": 1.0,
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
            "truth_key": truth_key,
            "truth_prefix": truth_key,
            "truth_cardinality": RelationshipCardinality.MULTI_ACTIVE.value,
            "status": RelationshipStatus.ACTIVE.value,
            "memory_type": MemoryType.ROLLUP.value,
            "relationship_type": "ROLLUP",
            "episode_uuid": next(iter(episode_uuids)),
            "episode_uuids": sorted(episode_uuids),
            "observed_count": 1,
            "created_by": "epoch-session-digest",
            "derived_from": fact_uuids,
            "epoch_id": active_epoch_id,
            "metadata": {"session_id": session_id, "kind": "session_digest"},
        },
        valid_from=min(reference_times),
    )
