"""The utility read model: a pure projection over append-only events.

The largest of the shared planes, 144 source lines that were two copies, and the
one with the strongest reason to be one copy: this is a DERIVED read model. It
touches no fact-plane field and issues no SQL. Its inputs are ``use_events``,
``outcome_events`` and the receipt ledger; its output is a list of
``MemoryUtilityProjection``. Everything engine-specific happens before it is
called.

Why the duplication was dangerous rather than merely wasteful
-------------------------------------------------------------
``scripts/verify/parity_coverage.py`` opens with the case: the Postgres copy of
``_utility_projection_from_events`` keyed the ``anchor`` half-life on
``"identity"``, so anchor memories decayed **4x faster on Postgres** — 90 days
instead of 365 — under a docstring claiming byte-identical behaviour. A
3,420-line parity suite passed, because it drove ``utility_projection`` with two
memory types whose half-lives happen to match on both engines.

That bug is a divergence between two copies of a table literal. It is not
possible to write here, because there is one table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from memotron.models import (
    MemoryScope,
    MemoryUtilityProjection,
    OutcomeEvent,
    UseEvent,
    UseEventKind,
)
from memotron.storage.receipts import ReceiptDecisionType, payload_digest

if TYPE_CHECKING:
    from memotron.storage._shared._protocol import SharedPlaneBackend

    _Base = SharedPlaneBackend
else:
    _Base = object


class UtilityProjectionPlaneMixin(_Base):
    """Rebuild the utility read model from events, or from receipts."""

    def utility_projection(
        self, *, scope_key: str, relationship_uuid: str | None = None, as_of: datetime | None = None
    ) -> list[MemoryUtilityProjection]:
        """Rebuild the utility read model solely from append-only use/outcome events."""
        now = as_of or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("utility projection as_of must be timezone-aware")
        uses = [
            event
            for event in self.use_events(scope_key=scope_key, relationship_uuid=relationship_uuid)
            if event.used_at <= now
        ]
        outcomes = [event for event in self.outcome_events(scope_key=scope_key) if event.judged_at <= now]
        return self._utility_projection_from_events(
            scope_key=scope_key,
            uses=uses,
            outcomes=outcomes,
            relationship_uuid=relationship_uuid,
            as_of=now,
        )

    def utility_projection_from_receipts(
        self, *, scope_key: str, relationship_uuid: str | None = None, as_of: datetime | None = None
    ) -> list[MemoryUtilityProjection]:
        """Rebuild utility directly from hash-linked event receipts.

        The engine's event tables are an operational index only.  This method
        deliberately ignores them and replays the immutable receipt payloads,
        making event-plane audit and projection recovery independently
        verifiable.
        """
        now = as_of or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("utility projection as_of must be timezone-aware")
        uses: list[UseEvent] = []
        outcomes: list[OutcomeEvent] = []
        seen_use_ids: set[str] = set()
        seen_outcome_ids: set[str] = set()
        for receipt in self.receipts.receipts_for_scope(scope_key):
            if receipt.decision_type not in {
                ReceiptDecisionType.USE_EVENT_RECORDED,
                ReceiptDecisionType.OUTCOME_EVENT_RECORDED,
            }:
                continue
            if receipt.event_payload is None or receipt.event_payload_digest is None:
                raise ValueError(f"event receipt {receipt.receipt_uuid} lacks its canonical event payload")
            try:
                payload = json.loads(receipt.event_payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"event receipt {receipt.receipt_uuid} has invalid event payload JSON") from exc
            if payload_digest(payload) != receipt.event_payload_digest:
                raise ValueError(f"event receipt {receipt.receipt_uuid} payload digest mismatch")
            # One local per branch, deliberately. The pre-extraction copies reused a
            # single `event` name for both models, which mypy binds to `UseEvent` from
            # the first assignment and then reports seven times in the else-branch
            # (1 assignment, 5 attr-defined, 1 arg-type). Those seven were the entire
            # reason `attr-defined` was suppressed on the module -- and suppressing it
            # here would also switch off the only check that catches a
            # `SharedPlaneBackend` Protocol member a backend does not actually provide.
            # Splitting the name removes the errors instead of hiding them.
            if receipt.decision_type == ReceiptDecisionType.USE_EVENT_RECORDED:
                use_event = UseEvent.model_validate(payload)
                if receipt.use_event_id != use_event.use_id:
                    raise ValueError(f"use receipt {receipt.receipt_uuid} has mismatched use_event_id")
                if use_event.scope.key != scope_key:
                    raise ValueError(f"use receipt {receipt.receipt_uuid} has mismatched scope")
                if use_event.use_id in seen_use_ids:
                    raise ValueError(f"duplicate use event receipt for {use_event.use_id}")
                seen_use_ids.add(use_event.use_id)
                if use_event.used_at <= now and (
                    relationship_uuid is None or use_event.relationship_uuid == relationship_uuid
                ):
                    uses.append(use_event)
            else:
                outcome_event = OutcomeEvent.model_validate(payload)
                if receipt.outcome_event_id != outcome_event.outcome_id:
                    raise ValueError(f"outcome receipt {receipt.receipt_uuid} has mismatched outcome_event_id")
                if outcome_event.scope.key != scope_key:
                    raise ValueError(f"outcome receipt {receipt.receipt_uuid} has mismatched scope")
                if outcome_event.outcome_id in seen_outcome_ids:
                    raise ValueError(f"duplicate outcome event receipt for {outcome_event.outcome_id}")
                seen_outcome_ids.add(outcome_event.outcome_id)
                if outcome_event.judged_at <= now:
                    outcomes.append(outcome_event)
        return self._utility_projection_from_events(
            scope_key=scope_key,
            uses=uses,
            outcomes=outcomes,
            relationship_uuid=relationship_uuid,
            as_of=now,
        )

    def _utility_projection_from_events(
        self,
        *,
        scope_key: str,
        uses: list[UseEvent],
        outcomes: list[OutcomeEvent],
        relationship_uuid: str | None,
        as_of: datetime,
    ) -> list[MemoryUtilityProjection]:
        """Build the derived read model without touching fact-plane fields."""
        outcomes_by_use: dict[str, list[OutcomeEvent]] = {}
        for outcome in outcomes:
            outcomes_by_use.setdefault(outcome.use_id, []).append(outcome)
        by_relationship: dict[str, list[UseEvent]] = {}
        for use in uses:
            by_relationship.setdefault(use.relationship_uuid, []).append(use)
        if ":" not in scope_key:
            raise ValueError(f"scope_key must be formatted as kind:id, got {scope_key!r}")
        scope_kind, scope_id = scope_key.split(":", 1)
        scope = MemoryScope(kind=scope_kind, scope_id=scope_id)
        projections: list[MemoryUtilityProjection] = []
        for uuid, relation_uses in by_relationship.items():
            relationship = self._memory_graph.get_relationship(uuid)
            memory_type = str(relationship.properties.get("memory_type", ""))
            # ONE table, deliberately. Two copies of this literal is the exact
            # shape of the `anchor` half-life divergence in the module docstring.
            half_life_seconds = {
                "state": 7 * 24 * 3600.0,
                "directive": 30 * 24 * 3600.0,
                "preference": 90 * 24 * 3600.0,
                "requirement": 180 * 24 * 3600.0,
                "anchor": 365 * 24 * 3600.0,
                "decision": 365 * 24 * 3600.0,
                "incident": 180 * 24 * 3600.0,
            }.get(memory_type, 90 * 24 * 3600.0)
            cited = [event for event in relation_uses if event.kind == UseEventKind.CITED_OR_USED]
            outcomes = [outcome for event in relation_uses for outcome in outcomes_by_use.get(event.use_id, [])]
            strengthening_times = [event.used_at for event in cited] + [
                outcome.judged_at for outcome in outcomes if outcome.verdict.value == "positive"
            ]
            stability = sum(
                2.0 ** (-max(0.0, (as_of - timestamp).total_seconds()) / half_life_seconds)
                for timestamp in strengthening_times
            )
            positive = sum(1 for outcome in outcomes if outcome.verdict.value == "positive")
            negative = sum(1 for outcome in outcomes if outcome.verdict.value in {"negative", "corrected"})
            retrieved = [event for event in relation_uses if event.kind == UseEventKind.RETRIEVED]
            injected = [event for event in relation_uses if event.kind == UseEventKind.INJECTED]
            projections.append(
                MemoryUtilityProjection(
                    relationship_uuid=uuid,
                    scope=scope,
                    use_stability=stability,
                    beta_positive=1.0 + positive,
                    beta_negative=1.0 + negative,
                    positive_outcome_count=positive,
                    negative_outcome_count=negative,
                    cited_or_used_count=len(cited),
                    impression_count=len(retrieved),
                    injected_count=len(injected),
                    last_used_at=max((event.used_at for event in cited), default=None),
                    last_injected_at=max((event.used_at for event in injected), default=None),
                    last_retrieved_at=max((event.used_at for event in retrieved), default=None),
                )
            )
        return sorted(projections, key=lambda projection: projection.relationship_uuid)
