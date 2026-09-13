"""Turning a scope into the prose an agent is given, inside a token budget.

Sixteen members, and the budget half is the subtle one. `_apply_token_budget` (150
lines) decides what gets DROPPED when a profile will not fit -- so a bug here does not
raise, it silently withholds a fact the agent needed. `_budget_shares_for_types` and
`_apply_type_share_cap` are the fairness rules that stop one memory type crowding out
the rest."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    Motive,
    ProfilePolicy,
)
from memotron.dreaming import DreamEngine
from memotron.models import (
    MemoryContextItem,
    MemoryProfile,
    MemoryProfileFact,
    MemoryScope,
)
from memotron.receipts import (
    ReceiptDecisionType,
)
from memotron.retrieval import (
    TemporalAuthorityCandidate,
    demote_contradicted_same_slot,
)
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    import memotron.context as context_module

    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class ProfileRenderMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def profile(
        self,
        *,
        scope: MemoryScope,
        policy: ProfilePolicy | None = None,
        as_of: datetime | None = None,
        token_budget: int | None = None,
        motive: Motive | str | None = None,
        reader_agent_id: str | None = None,
        maintain_context: bool = False,
        context_policy: context_module.ContextPolicy | None = None,
        context_transport: context_module.ContextTransport | None = None,
    ) -> MemoryProfile:
        """Build a scoped memory context block.

        Parameters
        ----------
        reader_agent_id:
            WS-19 T22: the calling agent for the per-memory
            ``visibility_agents`` allowlist.  Restricted rows are fail-closed
            against it (covering the pinned lead, static/dynamic fills, and
            the budget pass — all flow from one fact list).  ``None``
            (default) is the operator / SDK-owner context and sees everything.
        scope:
            The scope to build the profile for.
        policy:
            Optional policy override; falls back to ``config.profile``.
        as_of:
            Build a historical profile valid at this instant.
        token_budget:
            WS-5: Maximum approximate tokens for the rendered_context.  Uses a
            simple deterministic estimator: ``len(text) // 4`` chars-per-token.
            When set, a budget-aware pass fills highest-value facts first.
            Negative values raise ValueError.  None = no budget (legacy behaviour,
            count caps only).
        motive:
            WS-5: Optional Motive (or name resolved from ``config.memory_bank``)
            whose ``retrieval_budget_share`` drives per-type token allocation.
            Ignored when ``token_budget`` is None.
        maintain_context:
            When ``True``, serve the INCREMENTAL, LLM-maintained artifact from
            :mod:`memotron.context` (``get_context()``) instead of
            re-ranking and re-rendering every context-visible fact from
            scratch.  Default ``False`` is the exact legacy behaviour below,
            byte-for-byte — this flag is strictly additive.  Only supported
            for the current profile (``as_of=None``) and the unrestricted
            reader (``reader_agent_id=None``); the maintained artifact is one
            shared, scope-level artifact, not yet partitioned per historical
            instant or per restricted-visibility reader.
        context_policy:
            Optional :class:`memotron.context.ContextPolicy` override.
            Ignored unless ``maintain_context`` is ``True``.  When omitted, a
            default policy is used, with its ``token_budget`` taken from the
            ``token_budget`` argument when that is provided.
        context_transport:
            Optional :class:`memotron.context.ContextTransport` override
            (tests inject a fake here). Ignored unless ``maintain_context``
            is ``True``; defaults to
            :class:`memotron.context.OpenAICompatibleContextTransport`
            (the JedAI Gateway, ``claude-haiku-4-5``).
        """
        if token_budget is not None and token_budget < 0:
            raise ValueError("token_budget must be non-negative")
        self._require_authorized_scope(scope)

        profile_policy = policy or self.config.profile

        if maintain_context:
            import memotron.context as context_module

            if as_of is not None:
                raise ValueError("maintain_context does not support historical (as_of) profiles")
            if reader_agent_id is not None:
                raise ValueError(
                    "maintain_context does not support per-agent visibility scoping; "
                    "the maintained artifact is one shared, scope-level artifact"
                )
            facts = self._profile_facts(
                scope=scope,
                policy=profile_policy,
                as_of=None,
                reader_agent_id=None,
            )
            resolved_context_policy = context_policy or context_module.ContextPolicy(
                token_budget=token_budget if token_budget is not None else context_module.ContextPolicy().token_budget
            )
            resolved_transport = context_transport or context_module.OpenAICompatibleContextTransport()
            artifact = await context_module.get_context(
                graph=self.graph,
                scope=scope,
                facts=facts,
                policy=resolved_context_policy,
                transport=resolved_transport,
                now=datetime.now(UTC),
            )
            rendered = artifact.rendered_text
            return MemoryProfile(
                scope=scope,
                as_of=None,
                static_facts=facts,
                dynamic_facts=[],
                rendered_context=rendered,
                tokens_used=self._estimate_tokens(rendered),
                tokens_available=resolved_context_policy.token_budget,
                injected_items=[
                    MemoryContextItem(relationship_uuid=fact.relationship_uuid, fact=fact.fact, mode="injected")
                    for fact in facts
                    if fact.relationship_uuid in artifact.fact_marks
                ],
                reference_items=[],
            )

        # Resolve motive when a name string is provided.
        resolved_motive: Motive | None = None
        if motive is not None:
            resolved_motive = self.config.memory_bank.motive(motive) if isinstance(motive, str) else motive

        facts = self._profile_facts(
            scope=scope,
            policy=profile_policy,
            as_of=as_of,
            reader_agent_id=reader_agent_id,
        )
        # WS-20 T23: pinned facts are guaranteed inclusion BEFORE the
        # score-ranked fill — they bypass static/dynamic relationship-type
        # filters, max_static_facts, and per-type caps, and lead the static
        # section in deterministic (created_at, uuid) order.  Visibility was
        # already enforced by _profile_facts, so a superseded/pruned pin never
        # reaches this list.
        pinned_facts = sorted(
            (fact for fact in facts if fact.pinned),
            key=lambda fact: (
                self._datetime_sort_value(fact.created_at),
                fact.relationship_uuid,
            ),
        )
        unpinned_facts = [fact for fact in facts if not fact.pinned]
        static_facts = [
            fact
            for fact in unpinned_facts
            if self._profile_relationship_allowed(
                relationship_type=fact.relationship_type,
                allowed_relationship_types=profile_policy.static_relationship_types,
            )
        ]
        dynamic_facts = [
            fact
            for fact in unpinned_facts
            if self._profile_relationship_allowed(
                relationship_type=fact.relationship_type,
                allowed_relationship_types=profile_policy.dynamic_relationship_types,
            )
            and self._profile_fact_is_dynamic(
                fact=fact,
                policy=profile_policy,
                anchor=as_of or datetime.now(UTC),
            )
        ]
        static_facts.sort(
            key=lambda fact: (
                fact.confidence,
                fact.observed_count,
                self._datetime_sort_value(fact.last_seen_at or fact.valid_from),
                fact.relationship_uuid,
            ),
            reverse=True,
        )
        dynamic_facts.sort(
            key=lambda fact: (
                self._datetime_sort_value(fact.last_seen_at or fact.valid_from),
                fact.confidence,
                fact.observed_count,
                fact.relationship_uuid,
            ),
            reverse=True,
        )
        static_facts = static_facts[: profile_policy.max_static_facts]
        dynamic_facts = dynamic_facts[: profile_policy.max_dynamic_facts]

        # WS-5: apply per-type caps when configured.
        if profile_policy.per_type_max_facts:
            static_facts = self._apply_per_type_caps(static_facts, profile_policy.per_type_max_facts)
            dynamic_facts = self._apply_per_type_caps(dynamic_facts, profile_policy.per_type_max_facts)

        # WS-20 T23: pins lead the static section, ahead of the ranked fill —
        # count caps and per-type caps were deliberately not applied to them.
        static_facts = [*pinned_facts, *static_facts]

        # WS-5: budget-aware pass (only when token_budget is provided).
        tokens_used = 0
        overflow_facts: list[MemoryProfileFact] = []
        if token_budget is not None:
            static_facts, dynamic_facts, overflow_facts, tokens_used = self._apply_token_budget(
                scope=scope,
                static_facts=static_facts,
                dynamic_facts=dynamic_facts,
                pinned_facts=pinned_facts,
                token_budget=token_budget,
                policy=profile_policy,
                motive=resolved_motive,
            )
        rendered = self._render_profile_context(
            scope=scope,
            static_facts=static_facts,
            dynamic_facts=dynamic_facts,
            policy=profile_policy,
        )
        # Append JIT references for overflow facts when reference_mode is enabled.
        # WS-20 T23: budget-starved PINS degrade to REF lines even when
        # reference_mode is off — a pin is never silently dropped.
        referenced_facts = (
            overflow_facts if profile_policy.reference_mode else [fact for fact in overflow_facts if fact.pinned]
        )
        if referenced_facts:
            ref_lines = ["References (expand via memory_evidence):"]
            ref_lines.extend(self._render_profile_fact_reference(f) for f in referenced_facts)
            rendered = rendered + "\n" + "\n".join(ref_lines)
            # WS-11: reference-mode budget overflow is receipted under an operator run —
            # a single receipt naming the referenced (not-inlined) relationships ([0021]).
            overflow_uuids = [f.relationship_uuid for f in referenced_facts]
            operator_run = self._begin_operator_run(job_name="profile", scope_key=scope.key)
            self._emit_receipt(
                operator_run,
                decision_type=ReceiptDecisionType.RETRIEVAL_BUDGET_OVERFLOW_REFERENCED,
                decision_reason=f"referenced {len(overflow_uuids)} overflow facts: {','.join(overflow_uuids)}",
                decision_result="referenced",
                now=as_of or datetime.now(UTC),
                scope_key=scope.key,
            )
            self._checkpoint_operator_run(operator_run)
        # When no token_budget, tokens_used stays 0 (legacy).
        tokens_used = 0 if token_budget is None else self._estimate_tokens(rendered)
        return MemoryProfile(
            scope=scope,
            as_of=as_of,
            static_facts=static_facts,
            dynamic_facts=dynamic_facts,
            rendered_context=rendered,
            tokens_used=tokens_used,
            tokens_available=token_budget,
            injected_items=[
                MemoryContextItem(relationship_uuid=fact.relationship_uuid, fact=fact.fact, mode="injected")
                for fact in {fact.relationship_uuid: fact for fact in [*static_facts, *dynamic_facts]}.values()
            ],
            reference_items=[
                MemoryContextItem(relationship_uuid=fact.relationship_uuid, fact=fact.fact, mode="reference")
                for fact in overflow_facts
            ],
        )

    def _profile_facts(
        self,
        *,
        scope: MemoryScope,
        policy: ProfilePolicy,
        as_of: datetime | None,
        reader_agent_id: str | None = None,
    ) -> list[MemoryProfileFact]:
        facts: list[MemoryProfileFact] = []
        # WS-25 T3: parallel bookkeeping for the within-slot recency
        # tiebreaker — profile() is the other current-truth read surface the
        # WS-25 GOAL names ("the context brief"), alongside search_context's
        # own stage between visibility and rerank.  Pinned facts are excluded
        # (never demoted, never counted against another fact's cluster),
        # matching the search_context wiring's pin invariant.
        temporal_items: list[TemporalAuthorityCandidate] = []
        # WS-3×WS-4 §101 two-tier read: the DEFAULT (current) context-visible profile
        # seeks the bounded working set via the relationships_ctx_idx index
        # (scan-avoiding), decoupling profile cost from total store size.  Historical
        # (as_of) profiles need superseded/expired rows valid at that instant, so they
        # read the full store.  The validity-window and demotion filters below still apply
        # to BOTH paths, so the resulting fact set is identical to a full scan.
        if as_of is None:
            candidates = self.graph.context_visible_relationships(scope=scope)
        else:
            candidates = self.graph.relationships()
        for relationship in candidates:
            if relationship.type == "MENTIONS":
                continue
            if relationship.properties.get("scope_key") != scope.key:
                continue
            # WS-19 T22: per-memory agent allowlist — a restricted row (even a
            # pinned one) never enters an agent's profile; operator / SDK-owner
            # reads (reader None) are unaffected.
            if not self._agent_may_view_relationship(relationship.properties, reader_agent_id):
                continue
            status = self._relationship_status(relationship.properties.get("status"))
            if not self._relationship_is_visible(
                status=status,
                valid_from=relationship.valid_from,
                valid_to=relationship.valid_to,
                as_of=as_of,
                include_statuses=None,
            ):
                continue
            # WS-4: demoted members (active_in_context=False) are excluded from default
            # profile context.  MISSING flag = True (backward-compat: all existing rows
            # that predate WS-4 are treated as in-context).  Redundant for the index path
            # (already excluded) but required for the as_of full-store path.
            if relationship.properties.get("active_in_context") is False:
                # Archive demotion is a current-context concern.  An as-of
                # profile before the archive transition must still recover the
                # fact that was valid at that instant.
                archived_at = relationship.properties.get("archived_at")
                if not (
                    as_of is not None
                    and relationship.properties.get("archive_tier") is True
                    and isinstance(archived_at, str)
                    and datetime.fromisoformat(archived_at) > as_of
                ):
                    continue
            pinned = relationship.properties.get("pinned") is True
            confidence = float(relationship.properties.get("confidence", 0.0))
            # WS-20 T23: a pin is a guaranteed-retrieval hold — the policy
            # confidence filter cannot silently drop it (visibility rules above
            # still apply, so a superseded/pruned pin does not surface).
            if confidence < policy.min_confidence and not pinned:
                continue
            subject = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.source_uuid).properties["name"])
            )
            object_value = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.target_uuid).properties["name"])
            )
            facts.append(
                MemoryProfileFact(
                    relationship_uuid=relationship.uuid,
                    relationship_type=relationship.type,
                    fact=str(self.graph.reveal(scope.key, relationship.properties["fact"])),
                    scope=scope,
                    subject=subject,
                    predicate=str(relationship.properties["predicate"]),
                    object=object_value,
                    confidence=confidence,
                    status=status,
                    valid_from=relationship.valid_from,
                    valid_to=relationship.valid_to,
                    last_seen_at=self._relationship_last_seen_at(relationship.properties, relationship.valid_from),
                    episode_uuids=self._relationship_episode_uuids(relationship.properties),
                    observed_count=int(relationship.properties.get("observed_count", 1)),
                    created_by=str(relationship.properties.get("created_by", "")),
                    metadata=self._relationship_metadata(relationship.properties) if policy.include_metadata else {},
                    memory_type=DreamEngine.memory_type_for_relationship(dict(relationship.properties)),
                    pinned=pinned,
                    created_at=relationship.created_at,
                )
            )
            if not pinned:
                temporal_items.append(
                    TemporalAuthorityCandidate(
                        uuid=relationship.uuid,
                        truth_slot=relationship.properties.get("truth_prefix"),
                        object_text=object_value,
                        polarity=str(relationship.properties.get("semantic_polarity", "positive")),
                        valid_from=relationship.valid_from,
                        created_at=relationship.created_at,
                    )
                )
        demoted = demote_contradicted_same_slot(temporal_items)
        if demoted:
            facts = [fact for fact in facts if fact.relationship_uuid not in demoted]
        return facts

    def _profile_relationship_allowed(
        self,
        *,
        relationship_type: str,
        allowed_relationship_types: tuple[str, ...] | None,
    ) -> bool:
        if allowed_relationship_types is None:
            return True
        return relationship_type in set(allowed_relationship_types)

    def _profile_fact_is_dynamic(
        self,
        *,
        fact: MemoryProfileFact,
        policy: ProfilePolicy,
        anchor: datetime,
    ) -> bool:
        if policy.dynamic_window_seconds is None:
            return True
        observed_at = fact.last_seen_at or fact.valid_from
        if observed_at is None:
            return True
        return (anchor - observed_at).total_seconds() <= policy.dynamic_window_seconds

    def _relationship_last_seen_at(
        self,
        properties: dict[str, Any],
        valid_from: datetime | None,
    ) -> datetime | None:
        last_seen_at = properties.get("last_seen_at")
        if isinstance(last_seen_at, str):
            return datetime.fromisoformat(last_seen_at)
        return valid_from

    def _render_profile_context(
        self,
        *,
        scope: MemoryScope,
        static_facts: list[MemoryProfileFact],
        dynamic_facts: list[MemoryProfileFact],
        policy: ProfilePolicy | None = None,
    ) -> str:
        """Render facts as an agent-ready context string.

        render_mode == "legacy" (default):
            Produces the exact legacy format:
                Memory profile for {scope.key}
                Static facts:
                - [TYPE] fact (confidence=..., observed_count=...)
                Recent facts:
                - [TYPE] fact (...)
            This path is byte-for-byte identical to the pre-WS-5 output.

        render_mode == "typed":
            Groups all facts (static + dynamic) by memory_type under clear headings.
            Ordering: ROLLUP type first (WS-4 hook), then identity, requirement,
            preference, state, decision, incident, directive, then any unknown types.
            Static-section facts with no dynamic counterpart are rendered once.
        """
        render_mode = policy.render_mode if policy is not None else "legacy"
        if render_mode == "typed":
            return self._render_profile_context_typed(
                scope=scope,
                static_facts=static_facts,
                dynamic_facts=dynamic_facts,
                policy=policy,
            )
        # Legacy path — byte-for-byte identical to pre-WS-5.
        lines = [f"Memory profile for {scope.key}", "Static facts:"]
        if static_facts:
            lines.extend(self._render_profile_fact(fact) for fact in static_facts)
        else:
            lines.append("- None")
        lines.append("Recent facts:")
        if dynamic_facts:
            lines.extend(self._render_profile_fact(fact) for fact in dynamic_facts)
        else:
            lines.append("- None")
        return "\n".join(lines)

    def _render_profile_context_typed(
        self,
        *,
        scope: MemoryScope,
        static_facts: list[MemoryProfileFact],
        dynamic_facts: list[MemoryProfileFact],
        policy: ProfilePolicy | None = None,
    ) -> str:
        """Typed render mode: groups facts by memory_type under headings.

        Type ordering (rollup-first hook for WS-4, no WS-4 dependency):
            rollup > identity > requirement > preference > state > decision > incident > directive > unknown
        """
        # Merge static + dynamic into a single deduplicated ordered set.
        seen_uuids: set[str] = set()
        all_facts: list[MemoryProfileFact] = []
        for fact in static_facts:
            if fact.relationship_uuid not in seen_uuids:
                seen_uuids.add(fact.relationship_uuid)
                all_facts.append(fact)
        for fact in dynamic_facts:
            if fact.relationship_uuid not in seen_uuids:
                seen_uuids.add(fact.relationship_uuid)
                all_facts.append(fact)

        # Priority order for memory types.  "rollup" sorts first (WS-4 hook).
        type_order = ["rollup", "anchor", "requirement", "preference", "state", "decision", "incident", "directive"]
        type_rank: dict[str, int] = {t: i for i, t in enumerate(type_order)}

        # Group by memory_type.
        grouped: dict[str, list[MemoryProfileFact]] = {}
        for fact in all_facts:
            key = (fact.memory_type or "unknown").lower()
            grouped.setdefault(key, []).append(fact)

        # Sort type groups by priority order.
        ordered_keys = sorted(grouped.keys(), key=lambda k: type_rank.get(k, len(type_order)))

        lines = [f"Memory profile for {scope.key} [typed]"]
        for type_key in ordered_keys:
            heading = type_key.upper()
            lines.append(f"{heading}:")
            lines.extend(self._render_profile_fact(fact) for fact in grouped[type_key])

        if len(lines) == 1:
            lines.append("- None")
        return "\n".join(lines)

    @staticmethod
    def _profile_fact_line(
        *,
        relationship_type: str,
        fact: str,
        confidence: float,
        observed_count: int,
        pinned: bool,
    ) -> str:
        """The ONE profile fact line format — shared by the profile renderer and
        the WS-22 T28 token-savings measurement so both count identical bytes."""
        marker = "[PINNED] " if pinned else ""
        return f"- {marker}[{relationship_type}] {fact} (confidence={confidence:.2f}, observed_count={observed_count})"

    def _render_profile_fact(self, fact: MemoryProfileFact) -> str:
        # WS-20 T23: pinned facts are visibly marked; unpinned rendering is
        # byte-for-byte the legacy line.
        return self._profile_fact_line(
            relationship_type=fact.relationship_type,
            fact=fact.fact,
            confidence=fact.confidence,
            observed_count=fact.observed_count,
            pinned=fact.pinned,
        )

    def _render_profile_fact_reference(self, fact: MemoryProfileFact) -> str:
        """WS-5: Lightweight JIT reference line for overflow facts."""
        memory_type = fact.memory_type or "unknown"
        label = fact.fact[:80] + ("..." if len(fact.fact) > 80 else "")
        return f"[REF:{memory_type}] {fact.relationship_uuid} — {label}"

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Deterministic token estimator: approximately 4 characters per token.

        This is a simple, hermetic, offline-safe estimator.  It is intentionally
        conservative (rounds up) so callers that set a budget get a safe lower
        bound on how many tokens the rendered_context will consume.  The
        estimator is *deterministic* — same text always → same count.
        """
        return (len(text) + 3) // 4  # ceiling division

    _TYPE_ORDER: ClassVar[list[str]] = [
        "rollup",
        "anchor",
        "requirement",
        "preference",
        "state",
        "decision",
        "incident",
        "directive",
    ]

    def _budget_shares_for_types(
        self,
        types: list[str],
        motive: Motive | None,
        policy: ProfilePolicy,
    ) -> dict[str, float]:
        """Return fractional budget shares (summing to 1.0) keyed by memory_type.

        Priority:
            1. Motive.retrieval_budget_share (a single float for the Motive's
               types — distributed evenly across motive's allowed_memory_types).
            2. Even split across all present types (fallback).

        WS-24 type CAP: no single type may exceed ``policy.max_type_budget_share``.
        Excess is redistributed to the types still under the cap, iterating to a
        fixpoint so redistribution cannot push a recipient over its own cap.

        This replaces the WS-20 T24 per-type FLOOR.  A floor guaranteed a named
        type a minimum share of the budget, which is a claim on context that no
        knowledge-representation tradition attaches to a type — and it is the
        precise mechanism that turned a classification error into a retrieval
        failure: 148 field-inventory facts landed in ``identity`` and then held
        the share ``identity`` was guaranteed.  A cap can only ever shrink one
        type's claim, never invent relevance for a type with nothing to say.
        """
        if not types:
            return {}

        # Base: even split.
        base_share = 1.0 / len(types)
        shares: dict[str, float] = dict.fromkeys(types, base_share)

        if motive is not None and motive.retrieval_budget_share is not None and motive.allowed_memory_types:
            motive_types = {mt.value for mt in motive.allowed_memory_types}
            in_motive = [t for t in types if t in motive_types]
            out_motive = [t for t in types if t not in motive_types]
            if in_motive:
                # Give retrieval_budget_share fraction to in-motive types, rest to others.
                motive_total = motive.retrieval_budget_share
                other_total = 1.0 - motive_total
                for t in in_motive:
                    shares[t] = motive_total / len(in_motive)
                for t in out_motive:
                    shares[t] = other_total / len(out_motive) if out_motive else 0.0

        # Normalize before capping so the cap is read against real shares.
        total = sum(shares.values())
        if total > 0:
            shares = {t: v / total for t, v in shares.items()}

        shares = self._apply_type_share_cap(shares, policy.max_type_budget_share)

        # Normalize to ensure sum == 1.0 (floating point).
        total = sum(shares.values())
        if total > 0:
            shares = {t: v / total for t, v in shares.items()}
        return shares

    @staticmethod
    def _apply_type_share_cap(shares: dict[str, float], cap: float | None) -> dict[str, float]:
        """WS-24: clamp every type's budget share to *cap*, spreading the excess.

        Inert when ``cap`` is None, when one type is present (there is nowhere
        to redistribute to, and capping the only type would just discard
        budget), or when ``cap`` is already unreachable given the type count
        (``cap * len(types) <= 1`` means no assignment can satisfy it, so the
        even split stands rather than silently collapsing to it).
        """
        if cap is None or len(shares) <= 1:
            return shares
        if cap * len(shares) <= 1.0:
            return shares
        capped = dict(shares)
        # Fixpoint: redistributing to under-cap types can push one over, so
        # repeat until nothing exceeds the cap.  Bounded by the type count.
        for _ in range(len(capped)):
            over = {t: v for t, v in capped.items() if v > cap}
            if not over:
                break
            excess = sum(v - cap for v in over.values())
            headroom = {t: cap - v for t, v in capped.items() if t not in over and v < cap}
            room = sum(headroom.values())
            if room <= 0:
                break
            for t in over:
                capped[t] = cap
            granted = min(excess, room)
            for t, available in headroom.items():
                capped[t] += granted * (available / room)
        return capped

    def _apply_per_type_caps(
        self,
        facts: list[MemoryProfileFact],
        per_type_max_facts: dict[str, int],
    ) -> list[MemoryProfileFact]:
        """Apply per-memory-type caps to a fact list, preserving order."""
        type_counts: dict[str, int] = {}
        result: list[MemoryProfileFact] = []
        for fact in facts:
            type_key = (fact.memory_type or "unknown").lower()
            cap = per_type_max_facts.get(type_key)
            if cap is not None:
                count = type_counts.get(type_key, 0)
                if count >= cap:
                    continue
                type_counts[type_key] = count + 1
            result.append(fact)
        return result

    def _apply_token_budget(
        self,
        *,
        scope: MemoryScope,
        static_facts: list[MemoryProfileFact],
        dynamic_facts: list[MemoryProfileFact],
        token_budget: int,
        policy: ProfilePolicy,
        motive: Motive | None,
        pinned_facts: list[MemoryProfileFact] | None = None,
    ) -> tuple[list[MemoryProfileFact], list[MemoryProfileFact], list[MemoryProfileFact], int]:
        """Budget-aware pass: fill highest-value facts first up to token_budget.

        Algorithm
        ---------
        0. Pins first (WS-20 T23): ``pinned_facts`` consume the budget before
           any score-ranked fill, in their given deterministic (created_at,
           uuid) order.  A pin the budget cannot hold overflows for REF-line
           rendering — it is never silently dropped.
        1. Merge all candidate facts (static + dynamic, deduped by uuid) in
           value-descending order (salience proxy: confidence × observed_count,
           then recency, then uuid tiebreak).
        2. Compute per-type budget shares (via _budget_shares_for_types).
        3. Type cap (WS-24): no type's share exceeds
           ``policy.max_type_budget_share``; the excess is redistributed to the
           types still under the cap.  There is no per-type floor.
        4. Fill each type's share greedily, highest-value first, up to per-type
           token budget.  Overflow facts are returned for optional JIT reference
           rendering by the caller (reference_mode).
        5. Rebuild static_facts / dynamic_facts from the selected set preserving
           original ordering.
        6. Approximate tokens_used using the same estimator so callers can check
           budget before rendering.

        Returns
        -------
        (new_static, new_dynamic, overflow_facts, tokens_used)
            overflow_facts — facts that did not fit; empty when all facts fit.
            tokens_used   — approximate token count for the inlined facts only
                            (caller adds references overhead separately).

        Token estimator: ``(len(text) + 3) // 4`` (ceiling 4 chars/token).
        This is deterministic, offline-safe, and intentionally conservative.
        """
        pinned = list(pinned_facts or ())
        pinned_uuids = {fact.relationship_uuid for fact in pinned}

        # Unique candidate pool (static preferred over dynamic for dedup);
        # pins are handled by the budget-first pass below, never re-ranked.
        seen_uuids: set[str] = set(pinned_uuids)
        candidates: list[MemoryProfileFact] = []
        for fact in static_facts:
            if fact.relationship_uuid not in seen_uuids:
                seen_uuids.add(fact.relationship_uuid)
                candidates.append(fact)
        for fact in dynamic_facts:
            if fact.relationship_uuid not in seen_uuids:
                seen_uuids.add(fact.relationship_uuid)
                candidates.append(fact)

        # Sort by value: confidence × observed_count descending, then recency, uuid.
        candidates.sort(
            key=lambda f: (
                f.confidence * f.observed_count,
                self._datetime_sort_value(f.last_seen_at or f.valid_from),
                f.relationship_uuid,
            ),
            reverse=True,
        )

        # Group by memory_type.
        by_type: dict[str, list[MemoryProfileFact]] = {}
        for fact in candidates:
            type_key = (fact.memory_type or "unknown").lower()
            by_type.setdefault(type_key, []).append(fact)

        present_types = list(by_type.keys())
        shares = self._budget_shares_for_types(present_types, motive, policy)

        # Compute token overhead of the profile header (reserved).
        header_text = (
            f"Memory profile for {scope.key} [typed]\n"
            if policy.render_mode == "typed"
            else f"Memory profile for {scope.key}\nStatic facts:\nRecent facts:\n"
        )
        header_tokens = self._estimate_tokens(header_text)
        remaining_budget = max(0, token_budget - header_tokens)

        # WS-20 T23: pins consume the budget FIRST, in deterministic order.
        # A pin that no longer fits overflows (REF line) — never dropped.
        selected_uuids_pinned: set[str] = set()
        pinned_overflow: list[MemoryProfileFact] = []
        pinned_tokens = 0
        for fact in pinned:
            fact_tokens = self._estimate_tokens(self._render_profile_fact(fact) + "\n")
            if pinned_tokens + fact_tokens <= remaining_budget:
                selected_uuids_pinned.add(fact.relationship_uuid)
                pinned_tokens += fact_tokens
            else:
                pinned_overflow.append(fact)
        remaining_budget = max(0, remaining_budget - pinned_tokens)

        # Per-type token budgets.  Shares are already capped (WS-24): no type
        # can hold more than ``policy.max_type_budget_share`` of the budget, so
        # a single over-populated type cannot crowd every other type out of
        # context.  There is deliberately no per-type FLOOR any more — a
        # guaranteed minimum share is what let 148 misclassified facts hold the
        # budget the type was promised.  Which facts earn their tokens is the
        # rerank's job; which facts are always present is what ``pinned`` is
        # for, per fact and operator-set.
        type_budgets: dict[str, int] = {t: max(1, int(remaining_budget * shares.get(t, 0.0))) for t in present_types}

        # Greedy fill per type (priority order).
        selected_uuids: set[str] = set()
        overflow_facts: list[MemoryProfileFact] = []

        priority_order = sorted(
            present_types,
            key=lambda t: self._TYPE_ORDER.index(t) if t in self._TYPE_ORDER else len(self._TYPE_ORDER),
        )
        tokens_from_facts = 0
        for type_key in priority_order:
            type_budget = type_budgets.get(type_key, 0)
            used = 0
            for fact in by_type[type_key]:
                fact_text = self._render_profile_fact(fact) + "\n"
                fact_tokens = self._estimate_tokens(fact_text)
                if used + fact_tokens <= type_budget:
                    selected_uuids.add(fact.relationship_uuid)
                    used += fact_tokens
                    tokens_from_facts += fact_tokens
                else:
                    overflow_facts.append(fact)

        selected_set = selected_uuids | selected_uuids_pinned

        # Rebuild static and dynamic from selected set, preserving original ordering
        # (pins were prepended to static_facts by the caller, so they stay first).
        new_static = [f for f in static_facts if f.relationship_uuid in selected_set]
        new_dynamic = [f for f in dynamic_facts if f.relationship_uuid in selected_set]

        # Budget-starved pins overflow FIRST in the returned list so REF
        # rendering keeps the deterministic pin order.
        overflow_facts = [*pinned_overflow, *overflow_facts]

        tokens_used = header_tokens + pinned_tokens + tokens_from_facts
        return new_static, new_dynamic, overflow_facts, tokens_used
