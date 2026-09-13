#!/usr/bin/env python
"""Show the Memotron demo memory graph — and how it changed.

    uv run demo/inspect.py                                  # snapshot, print only
    uv run demo/inspect.py --save baseline                  # snapshot + remember it
    uv run demo/inspect.py --diff baseline                  # snapshot vs. a saved one
    uv run demo/inspect.py --diff baseline --save after-dream-1
    uv run demo/inspect.py --all-scopes                     # + personal / continuity

Snapshots are plain JSON under demo/snapshots/. The BEFORE -> AFTER delta across
a dreaming session is the demo: facts formed, one fact superseded by a
correction, one fact reinforced without a duplicate row, a theme absorbing its
members, and the token bill falling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _demopath  # noqa: F401  MUST BE FIRST — unshadows stdlib `inspect`
from _demo import (
    DEMO_AGENT_ID,
    DEMO_GRAPH_PATH,
    DEMO_PROJECT_ID,
    SNAPSHOT_DIR,
    banner,
    build_demo_platform,
    fail,
    kv,
    section,
    short,
    table,
    terminal_width_note,
)

PROOF_FIELDS = (
    ("episode_count", "raw episodes ingested"),
    ("processed_episode_count", "episodes dreamed"),
    ("pending_episode_count", "episodes still queued"),
    ("active_relationship_count", "active facts (incl. demoted)"),
    ("context_visible_relationship_count", "context-visible facts"),
    ("theme_relationship_count", "themes"),
    ("demoted_relationship_count", "demoted members"),
    ("created_relationship_count", "lifetime creates"),
    ("reinforced_relationship_count", "lifetime reinforcements"),
    ("superseded_relationship_count", "lifetime supersessions"),
    ("pruned_relationship_count", "lifetime prunes"),
    ("dream_run_count", "dream runs"),
)

TOKEN_FIELDS = (
    ("tokens_raw_episodes", "raw episodes (the no-memory baseline)"),
    ("tokens_unbudgeted_facts", "every context-visible fact, no budget"),
    ("tokens_rendered_profile", "the profile actually injected"),
    ("tokens_saved_vs_raw", "saved per task vs. re-reading raw"),
    ("tokens_saved_by_demotion", "saved by theme / duplicate demotion"),
)

RATE_FIELDS = (
    ("compression_ratio", "episodes per context-visible fact"),
    ("semantic_dedup_rate", "observations that reinforced vs. created"),
    ("repeat_search_rate", "queries re-asked across session boundaries"),
    ("answered_from_profile_rate", "citations answered by the profile, not a search"),
)


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


async def collect(platform, scope_names: list[str]) -> dict[str, Any]:
    client = platform.client
    structure = _structural_properties(client)

    scopes = {"project": platform.project_scope}
    if "personal" in scope_names and platform.user_scope is not None:
        scopes["personal"] = platform.user_scope
    if "continuity" in scope_names:
        scopes["continuity"] = platform.agent_scope(DEMO_AGENT_ID)

    snapshot: dict[str, Any] = {
        "taken_at": datetime.now(UTC).isoformat(),
        "tenant": DEMO_PROJECT_ID,
        "graph_path": str(DEMO_GRAPH_PATH),
        "scopes": {},
    }
    for name, scope in scopes.items():
        proof = await client.memory_evolution(scope=scope)
        facts: dict[str, Any] = {}
        for fact in [*proof.active_facts, *proof.inactive_facts]:
            props = structure.get(fact.relationship_uuid, {})
            facts[fact.relationship_uuid] = {
                "type": fact.memory_type or "-",
                "relationship_type": fact.relationship_type,
                "subject": fact.subject,
                "predicate": fact.predicate,
                "object": fact.object,
                "fact": fact.fact,
                "confidence": round(fact.confidence, 3),
                "observed_count": fact.observed_count,
                "status": fact.status.value,
                "superseded_by": fact.superseded_by_relationship_uuid or "",
                "active_in_context": props.get("active_in_context", True) is not False,
                "summarized_by": str(props.get("summarized_by") or ""),
                "duplicate_of": str(props.get("duplicate_of") or ""),
                "theme_depth": int(props.get("theme_depth", 0) or 0),
                "valid_to": fact.valid_to.isoformat() if fact.valid_to else "",
            }
        snapshot["scopes"][name] = {
            "scope_key": scope.key,
            "facts": facts,
            "proof": proof.model_dump(mode="json", exclude={"active_facts", "inactive_facts", "signals"}),
        }
    return snapshot


def _structural_properties(client) -> dict[str, dict[str, Any]]:
    """Relationship flags memory_evolution does not surface (they are row
    properties, not the `metadata` blob): context demotion, theme membership,
    theme depth, cross-prefix duplicates."""

    export = client.export_graph()
    out: dict[str, dict[str, Any]] = {}
    for rel in export["relationships"]:
        props = rel.get("properties", {})
        out[rel["uuid"]] = {
            "active_in_context": props.get("active_in_context", True),
            "summarized_by": props.get("summarized_by"),
            "duplicate_of": props.get("duplicate_of"),
            "theme_depth": props.get("theme_depth", 0),
        }
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render(snapshot: dict[str, Any], label: str) -> None:
    banner(
        "MEMOTRON DEMO — MEMORY GRAPH",
        f"tenant {snapshot['tenant']}   taken {snapshot['taken_at'][:19]}Z" + (f"   label {label}" if label else ""),
    )
    kv("graph file", snapshot["graph_path"])
    terminal_width_note()

    for name, data in snapshot["scopes"].items():
        _render_scope(name, data)


def _render_scope(name: str, data: dict[str, Any]) -> None:
    facts: dict[str, Any] = data["facts"]
    proof: dict[str, Any] = data["proof"]

    banner(f"SCOPE: {name.upper()}", data["scope_key"])

    themes = {u: f for u, f in facts.items() if f["type"] == "theme" and f["status"] == "active"}
    visible = {
        u: f for u, f in facts.items() if f["status"] == "active" and f["active_in_context"] and f["type"] != "theme"
    }
    demoted = {u: f for u, f in facts.items() if f["status"] == "active" and not f["active_in_context"]}
    historical = {u: f for u, f in facts.items() if f["status"] != "active"}

    section("Active facts in context")
    table(
        ["id", "type", "subject", "predicate", "object", "conf", "obs"],
        [
            [
                short(u),
                f["type"],
                f["subject"],
                f["predicate"],
                f["object"],
                f"{f['confidence']:.2f}",
                str(f["observed_count"]),
            ]
            for u, f in sorted(visible.items(), key=lambda kv: (kv[1]["type"], kv[1]["subject"]))
        ],
    )
    print(
        f"  context-visible = {len(visible)} fact(s) + {len(themes)} theme(s) "
        f"= {proof.get('context_visible_relationship_count', 0)}"
        f"   ({len(demoted)} demoted, {len(historical)} historical)"
    )

    section("Themes (consolidation) and the members they absorbed")
    if not themes:
        print("  (none — consolidation has not synthesized a theme yet)")
    for theme_uuid, theme in themes.items():
        print(f"  THEME {short(theme_uuid)}  depth {theme['theme_depth']}  conf {theme['confidence']:.2f}")
        print(f"        {theme['fact']}")
        members = [(u, f) for u, f in facts.items() if f["summarized_by"] == theme_uuid]
        for member_uuid, member in members:
            state = "demoted from context" if not member["active_in_context"] else "in context"
            print(f"          - {short(member_uuid)}  {member['fact']}  [{state}]")
        if not members:
            print("          (members not resolvable in this snapshot)")

    section("Superseded / historical truth")
    rows = []
    for u, f in historical.items():
        successor = f["superseded_by"]
        rows.append(
            [
                short(u),
                f["type"],
                f["status"],
                f"{f['subject']} {f['predicate']} {f['object']}",
                f"-> {short(successor)}" if successor else "-",
            ]
        )
    table(["id", "type", "status", "fact (no longer current)", "successor"], rows)

    if demoted and not themes:
        section("Demoted rows (out of context, still searchable)")
        table(
            ["id", "type", "fact", "reason"],
            [
                [
                    short(u),
                    f["type"],
                    f["fact"],
                    "theme member" if f["summarized_by"] else ("duplicate" if f["duplicate_of"] else "?"),
                ]
                for u, f in demoted.items()
            ],
        )

    section("Evolution proof")
    for field, description in PROOF_FIELDS:
        kv(field, f"{proof.get(field, 0):<6} {description}", width=36)

    section("Rates")
    for field, description in RATE_FIELDS:
        value = proof.get(field)
        shown = "not measured" if value is None else f"{value:.3f}"
        kv(field, f"{shown:<14} {description}", width=36)

    section("Token savings (WS-22, deterministic estimator)")
    for field, description in TOKEN_FIELDS:
        kv(field, f"{proof.get(field, 0):<6} {description}", width=36)
    raw = proof.get("tokens_raw_episodes", 0)
    rendered = proof.get("tokens_rendered_profile", 0)
    if raw:
        pct = 100.0 * (raw - rendered) / raw
        kv("reduction", f"{pct:.1f}% fewer tokens than replaying the raw episodes", width=36)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def render_diff(old: dict[str, Any], new: dict[str, Any], old_label: str) -> None:
    banner(
        "DELTA — WHAT THIS DREAMING SESSION CHANGED",
        f"from '{old_label}' ({old['taken_at'][:19]}Z) to now ({new['taken_at'][:19]}Z)",
    )
    for name, new_data in new["scopes"].items():
        old_data = old["scopes"].get(name)
        if old_data is None:
            continue
        _diff_scope(name, old_data, new_data)


def _diff_scope(name: str, old_data: dict[str, Any], new_data: dict[str, Any]) -> None:
    old_facts: dict[str, Any] = old_data["facts"]
    new_facts: dict[str, Any] = new_data["facts"]

    section(f"{name}: facts")
    rows: list[list[str]] = []

    for uuid, fact in new_facts.items():
        before = old_facts.get(uuid)
        if before is None:
            rows.append(
                [
                    "+ NEW",
                    short(uuid),
                    fact["type"],
                    f"{fact['subject']} {fact['predicate']} {fact['object']}",
                ]
            )
            continue
        if before["status"] != fact["status"]:
            successor = fact["superseded_by"]
            rows.append(
                [
                    f"~ {before['status']}->{fact['status']}",
                    short(uuid),
                    fact["type"],
                    f"{fact['fact']}" + (f"   (superseded by {short(successor)})" if successor else ""),
                ]
            )
        if before["observed_count"] != fact["observed_count"]:
            rows.append(
                [
                    f"~ obs {before['observed_count']}->{fact['observed_count']}",
                    short(uuid),
                    fact["type"],
                    f"{fact['fact']}   (reinforced — no duplicate row)",
                ]
            )
        if abs(before["confidence"] - fact["confidence"]) > 1e-6:
            rows.append(
                [
                    f"~ conf {before['confidence']:.2f}->{fact['confidence']:.2f}",
                    short(uuid),
                    fact["type"],
                    fact["fact"],
                ]
            )
        if before["active_in_context"] and not fact["active_in_context"]:
            rows.append(
                [
                    "~ DEMOTED",
                    short(uuid),
                    fact["type"],
                    f"{fact['fact']}   (absorbed by theme {short(fact['summarized_by'])})",
                ]
            )
        if not before["active_in_context"] and fact["active_in_context"]:
            rows.append(["~ RE-PROMOTED", short(uuid), fact["type"], fact["fact"]])

    for uuid, fact in old_facts.items():
        if uuid not in new_facts:
            rows.append(["- GONE", short(uuid), fact["type"], fact["fact"]])

    table(["change", "id", "type", "fact"], rows)

    section(f"{name}: proof deltas")
    old_proof, new_proof = old_data["proof"], new_data["proof"]
    rows = []
    for field, description in (*PROOF_FIELDS, *TOKEN_FIELDS):
        before = old_proof.get(field, 0) or 0
        after = new_proof.get(field, 0) or 0
        if before == after:
            continue
        rows.append([field, str(before), str(after), f"{after - before:+d}", description])
    for field, description in RATE_FIELDS:
        before = old_proof.get(field)
        after = new_proof.get(field)
        if before == after:
            continue
        rows.append(
            [
                field,
                "n/a" if before is None else f"{before:.3f}",
                "n/a" if after is None else f"{after:.3f}",
                "",
                description,
            ]
        )
    table(["signal", "before", "after", "delta", "meaning"], rows)


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------


def snapshot_path(label: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in label)
    return SNAPSHOT_DIR / f"{safe}.json"


def load_snapshot(label: str) -> dict[str, Any]:
    path = snapshot_path(label)
    if not path.is_file():
        available = sorted(p.stem for p in SNAPSHOT_DIR.glob("*.json"))
        fail(
            f"no snapshot named '{label}' in {SNAPSHOT_DIR}\n"
            f"  available: {', '.join(available) if available else '(none yet)'}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def save_snapshot(label: str, snapshot: dict[str, Any]) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(label)
    payload = dict(snapshot, label=label)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    platform, _config = build_demo_platform()
    try:
        scope_names = ["project"]
        if args.all_scopes:
            scope_names += ["personal", "continuity"]
        snapshot = await collect(platform, scope_names)
    finally:
        platform.client.graph.close()

    render(snapshot, args.save or "")

    if args.diff:
        render_diff(load_snapshot(args.diff), snapshot, args.diff)

    if args.save:
        path = save_snapshot(args.save, snapshot)
        section("Snapshot saved")
        kv("label", args.save)
        kv("file", path)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the demo memory graph.")
    parser.add_argument("--save", default="", help="save this snapshot under a label")
    parser.add_argument("--diff", default="", help="diff against a previously saved label")
    parser.add_argument(
        "--all-scopes",
        action="store_true",
        help="also show the personal and session-continuity scopes",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
