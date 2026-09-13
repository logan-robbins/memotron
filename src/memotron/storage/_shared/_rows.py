"""Row decoders shared by both engines: a stored row in, a contract dict out.

Six functions, previously two copies each. They are pure functions of their
argument -- no ``self``, no connection, no engine -- and were already written
that way: four were ``@staticmethod`` on both engines, and the two that took
``self`` never used it.

Module-level functions rather than a mixin, following the precedent set by
:mod:`memotron.storage.postgres._common`, which moved ``as_json`` off
``GraphPlaneMixin`` for the same reason and recorded it: a helper that leaves the
mixin call graph entirely cannot re-create plane coupling from a new direction.
Decoding a row is not a governance concern or an epoch concern, so it should not
be reachable as ``self.something``.

What the two copies actually disagreed about
--------------------------------------------
Nothing in the bodies -- those compare equal after docstring stripping, which is
why they are here. The signatures disagreed, and only about the row type:
``sqlite3.Row`` on one side, ``dict[str, Any]`` on the other, for code that only
ever does ``row["column"]``. :class:`._protocol.Row` is that common interface,
so the shared decoder is checked against what the bodies really require instead
of against whichever concrete type its home file happened to import.

``canonical_visibility_agents`` is the clearest case in the set: the Postgres copy
was already a module-level function whose docstring read "Byte-identical to
``SQLiteStorageBackend._canonical_visibility_agents`` so the state-hash tuple is
the same string on both engines". The duplication was known, documented in prose,
and load-bearing for the receipt chain -- prose is now a shared definition.
"""

from __future__ import annotations

import json
from typing import Any

from memotron.storage._shared._protocol import Row


def epoch_row_to_dict(row: Row) -> dict[str, Any]:
    overrides_raw = row["overrides_json"]
    snapshot_raw = row["pre_adopt_snapshot_json"]
    return {
        "epoch_id": row["epoch_id"],
        "scope_key": row["scope_key"],
        "parent_epoch_id": row["parent_epoch_id"],
        "created_at": row["created_at"],
        "label": row["label"],
        "status": row["status"],
        "tier": row["tier"],
        "overrides": json.loads(overrides_raw) if overrides_raw else {},
        "shadow_store_path": row["shadow_store_path"],
        "pre_adopt_snapshot": json.loads(snapshot_raw) if snapshot_raw else None,
    }


def predicate_alias_row_to_dict(row: Row) -> dict[str, Any]:
    return {
        "scope_key": str(row["scope_key"]),
        "predicate_normalized": str(row["predicate_normalized"]),
        "canonical_predicate": str(row["canonical_predicate"]),
        "decided_by": str(row["decided_by"]),
        "embedding_identifier": (str(row["embedding_identifier"]) if row["embedding_identifier"] is not None else None),
        "cosine": float(row["cosine"]) if row["cosine"] is not None else None,
        "created_at": str(row["created_at"]),
        "epoch_id": str(row["epoch_id"]) if row["epoch_id"] else "",
    }


def entity_alias_row_to_dict(row: Row) -> dict[str, Any]:
    signals_raw = row["link_signals"]
    try:
        signals = json.loads(signals_raw) if isinstance(signals_raw, str) and signals_raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"entity_canon row {row['scope_key']}:{row['name_normalized']} carries invalid link_signals JSON"
        ) from exc
    return {
        "scope_key": str(row["scope_key"]),
        "name_normalized": str(row["name_normalized"]),
        "canonical_name": str(row["canonical_name"]),
        "status": str(row["status"]),
        "link_score": float(row["link_score"]) if row["link_score"] is not None else None,
        "link_signals": signals if isinstance(signals, dict) else {},
        "decided_by": str(row["decided_by"]),
        "embedding_identifier": (str(row["embedding_identifier"]) if row["embedding_identifier"] is not None else None),
        "proposed_at": str(row["proposed_at"]),
        "resolved_at": str(row["resolved_at"]) if row["resolved_at"] is not None else None,
        "resolved_by": str(row["resolved_by"]) if row["resolved_by"] is not None else None,
        "epoch_id": str(row["epoch_id"]) if row["epoch_id"] else "",
    }


def tenant_agent_from_row(row: Row) -> dict[str, Any]:
    return {
        "tenant_id": str(row["tenant_id"]),
        "agent_id": str(row["agent_id"]),
        "agent_name": str(row["name"]),
        "source": str(row["source"]),
        "created_at": str(row["created_at"]),
        "last_seen_at": str(row["last_seen_at"]),
    }


def tenant_prompt_version_from_row(row: Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "tenant_id": str(row["tenant_id"]),
        "version": str(row["version"]),
        "prompt_text": str(row["prompt_text"]),
        "motive_name": str(row["motive_name"]),
        "source_profile": str(row["source_profile"]),
        "source_profile_version": str(row["source_profile_version"]),
        "active": bool(row["active"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def canonical_visibility_agents(value: Any) -> list[str] | None:
    """WS-23 M2: order-stable projection of a row's ``visibility_agents``.

    ``None`` for a row without an allowlist (scope-default visibility) so
    pre-allowlist rows keep contributing a stable null; otherwise the sorted,
    de-duplicated agent ids — reordering an allowlist is not a state change,
    adding or removing a reader is.

    Both engines fold the result into their ``graph_state_hash`` state tuple, and
    the parity suite requires that hash to be the same STRING on both, so this
    being one definition rather than two matched copies is a correctness property
    of the receipt chain, not tidiness.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    return sorted({str(agent) for agent in value})
