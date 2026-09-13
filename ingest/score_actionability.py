"""Score a graph against ``ingest/actionability_goldset.yaml``.

This is the pilot's accept/reject gate. It replaces "does the graph look
reasonable" -- an eyeball judgement that cannot fail -- with a number that can.

THE STANDARD
------------
The gold set is absolute, not relative. A graph is judged on its own terms:

* did it store the facts that let an agent skip a step, and are they actually
  reachable from context (``must_remember``);
* did it keep out the facts that cost context and buy nothing
  (``must_not_remember``);
* how much of what it *did* store is neither (the noise ratio).

WHERE THE GATE FAILED: MISS vs QUARANTINED
------------------------------------------
A ``must_remember`` entry can fail in two very different ways, and collapsing
them hides the actual defect:

* **miss**       -- no fact matching it exists anywhere in the store. The
                    extractor never produced the candidate. Fix the extractor.
* **quarantined** -- a matching fact EXISTS but is not context-visible: it was
                    quarantined, demoted out of context, archived, pruned, or
                    superseded. The extractor worked and the gate misjudged it.
                    Fix the gate.

Both count as recall failures. They are reported separately because they send
you to different code.

ISOLATION
---------
Standalone by design: stdlib ``sqlite3`` plus PyYAML, and nothing from
``memotron``. The scorer must keep working while the engine is being
rewritten, and it must never be able to influence the thing it is measuring.
The graph is opened read-only (``mode=ro``).

QUARANTINE STORES ARE DISCOVERED, NOT ASSUMED
---------------------------------------------
The quarantine path is discovered at runtime by scanning ``sqlite_master`` for
tables whose name mentions quarantine, plus relationship-level quarantine
markers. If no quarantine store exists yet, the scorer degrades gracefully and
says so instead of failing.

USAGE
-----
    uv run ingest/score_actionability.py --graph portal-kb-v2
    uv run ingest/score_actionability.py --graph /path/to/graph.sqlite --show misses
    uv run ingest/score_actionability.py --verify-citations
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

INGEST_DIR = Path(__file__).resolve().parent
GRAPH_DIR = INGEST_DIR / ".memotron"
GOLDSET_PATH = INGEST_DIR / "actionability_goldset.yaml"
DEFAULT_PORTAL_ROOT = Path("/Users/logan.robbins/jedai/portal")

# Provenance edges, not claims -- they carry no fact text and must not be
# counted as things the graph chose to remember.
NON_FACT_RELATIONSHIP_TYPES = {"MENTIONS"}


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

_KEEP = re.compile(r"[^a-z0-9./:_@{}\-]+")
_TRIM = re.compile(r"^[.\-_:/]+|[.\-_:,;]+$")


def tokens_of(text: str) -> set[str]:
    """Normalized token set.

    Lowercases, drops markdown noise and currency/percent symbols, and keeps the
    characters that make identifiers identifiers -- dots, slashes, colons,
    underscores, hyphens, braces -- so that a URL, a dotted field path
    (``data.descriptions.offer_intro``), a templated path
    (``/content/preview/{locale}/{slug}/MarketingOffers/``) and a data store name
    each survive as ONE token rather than shattering into fragments that match
    everything.
    """
    if not text:
        return set()
    lowered = str(text).lower().replace("’", "'").replace("$", "").replace("%", "")
    lowered = lowered.replace("`", " ").replace("*", " ").replace("—", " ")
    raw = _KEEP.sub(" ", lowered).split()

    out: set[str] = set()
    for token in raw:
        token = _TRIM.sub("", token)
        if not token:
            continue
        out.add(token)
        # A URL should match with or without its scheme, and with or without a
        # trailing slash, since docs and extractors disagree about both.
        if "://" in token:
            out.add(token.split("://", 1)[1].rstrip("/"))
            out.add(token.rstrip("/"))
        if token.endswith("/"):
            out.add(token.rstrip("/"))
    return out


def _token_present(token: str, haystack: set[str]) -> bool:
    """Exact token, or a hyphen/underscore-bounded prefix of one.

    ``team`` matches ``team-level`` and ``team_admin``; it does not match
    ``teamwork``. This is what lets a gold phrase written in prose match a fact
    written as a compound, without opening the door to substring nonsense
    (``id`` must never match ``identity``).
    """
    if token in haystack:
        return True
    return any(candidate.startswith((f"{token}-", f"{token}_")) for candidate in haystack)


def phrase_in(phrase: str, haystack: set[str]) -> bool:
    """A phrase is contained when every one of its tokens is present."""
    wanted = tokens_of(phrase)
    return bool(wanted) and all(_token_present(token, haystack) for token in wanted)


# ---------------------------------------------------------------------------
# graph loading
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One thing the store is holding, and whether an agent can actually see it."""

    origin: str  # "graph" | "quarantine:<table>"
    fact: str
    subject: str
    predicate: str
    obj: str
    memory_type: str
    slug: str
    visible: bool
    reason: str  # why it is not visible, or "visible"
    subject_tokens: set[str] = field(default_factory=set)
    predicate_tokens: set[str] = field(default_factory=set)
    object_tokens: set[str] = field(default_factory=set)
    all_tokens: set[str] = field(default_factory=set)

    def index(self) -> None:
        self.subject_tokens = tokens_of(self.subject)
        self.predicate_tokens = tokens_of(self.predicate)
        self.object_tokens = tokens_of(self.obj)
        self.all_tokens = tokens_of(self.fact) | self.subject_tokens | self.predicate_tokens | self.object_tokens


def resolve_graph(value: str) -> Path:
    """Accept either a bare graph name or an explicit path."""
    candidate = Path(value).expanduser()
    if candidate.suffix == ".sqlite" or candidate.exists():
        path = candidate.resolve()
    else:
        path = (GRAPH_DIR / f"{value}.sqlite").resolve()
    if not path.exists():
        raise SystemExit(f"no graph at {path}")
    return path


def _visibility(props: dict[str, Any], valid_to: str | None) -> tuple[bool, str]:
    """Is this fact reachable from an agent's context?

    Stored is not the same as usable. A fact that has been quarantined, demoted
    out of the context tier, archived, pruned, superseded or time-bounded is
    still on disk but will never reach a prompt -- so for the purpose of "did
    the memory help", it is absent. The reason is retained because it is the
    diagnosis.
    """
    status = str(props.get("status") or "active").lower()
    if "quarantin" in status:
        return False, "quarantined"
    if status == "pruned" or props.get("pruned_at"):
        return False, "pruned"
    if status == "superseded" or props.get("superseded_at"):
        return False, "superseded"
    if status not in ("active", ""):
        return False, f"status={status}"
    if any("quarantin" in str(key).lower() for key in props if props.get(key)):
        return False, "quarantined"
    if props.get("archive_tier"):
        return False, "archived"
    if props.get("active_in_context") is False:
        return False, "demoted"
    if valid_to:
        return False, "expired"
    return True, "visible"


def load_graph_facts(path: Path) -> list[Candidate]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        nodes = {
            uuid: json.loads(properties or "{}")
            for uuid, properties in connection.execute("select uuid, properties_json from nodes")
        }
        rows = list(
            connection.execute("select type, source_uuid, target_uuid, properties_json, valid_to from relationships")
        )
    finally:
        connection.close()

    candidates: list[Candidate] = []
    for rel_type, source_uuid, target_uuid, properties, valid_to in rows:
        if rel_type in NON_FACT_RELATIONSHIP_TYPES:
            continue
        props = json.loads(properties or "{}")
        props.pop("embedding", None)
        props.pop("object_embedding", None)

        subject = str((nodes.get(source_uuid) or {}).get("name") or "")
        target_name = str((nodes.get(target_uuid) or {}).get("name") or "")
        obj = str(props.get("object") or props.get("object_surface") or target_name)
        predicate = str(props.get("predicate_canonical") or props.get("predicate") or rel_type)
        fact = str(props.get("fact") or f"{subject} {predicate} {obj}").strip()
        metadata = props.get("metadata") or {}
        slug = str(metadata.get("custom_id") or "") if isinstance(metadata, dict) else ""

        visible, reason = _visibility(props, valid_to)
        candidate = Candidate(
            origin="graph",
            fact=fact,
            subject=subject,
            predicate=predicate,
            obj=obj,
            memory_type=str(props.get("memory_type") or rel_type.lower()),
            slug=slug,
            visible=visible,
            reason=reason,
        )
        candidate.index()
        candidates.append(candidate)
    return candidates


_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _flatten(value: Any, sink: list[str]) -> None:
    if isinstance(value, str):
        stripped = value.strip()
        if _UUIDISH.match(stripped):
            # Row identifiers are not claim text; they only make the reported
            # "stored as" line unreadable.
            return
        if stripped.startswith(("{", "[")):
            try:
                _flatten(json.loads(stripped), sink)
                return
            except (ValueError, TypeError):
                pass
        sink.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _flatten(item, sink)
    elif isinstance(value, list):
        for item in value:
            _flatten(item, sink)
    elif value is not None:
        sink.append(str(value))


def load_quarantine(path: Path) -> tuple[list[Candidate], list[str]]:
    """Discover a quarantine store rather than assuming its shape.

    The gate's quarantine path is being built concurrently, so nothing here may
    depend on a specific table or column layout. Any table whose name mentions
    quarantine is read whole, every value (including nested JSON) is flattened
    into one text blob, and each row becomes a non-visible candidate. If nothing
    is found the scorer says so and carries on -- an absent quarantine store is
    a valid state, not an error.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    discovered: list[str] = []
    candidates: list[Candidate] = []
    try:
        tables = [
            name
            for (name,) in connection.execute("select name from sqlite_master where type='table'")
            if "quarantin" in name.lower()
        ]
        for table in tables:
            cursor = connection.execute(f'select * from "{table}"')
            columns = [description[0] for description in cursor.description]
            count = 0
            for row in cursor.fetchall():
                sink: list[str] = []
                for column, value in zip(columns, row, strict=False):
                    if column.lower().endswith("embedding"):
                        continue
                    _flatten(value, sink)
                blob = " ".join(sink)
                if not blob.strip():
                    continue
                candidate = Candidate(
                    origin=f"quarantine:{table}",
                    fact=blob,
                    subject="",
                    predicate="",
                    obj="",
                    memory_type="quarantined",
                    slug="",
                    visible=False,
                    reason="quarantined",
                )
                candidate.index()
                candidates.append(candidate)
                count += 1
            discovered.append(f"{table} ({count} rows)")
    finally:
        connection.close()
    return candidates, discovered


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


@dataclass
class Match:
    candidate: Candidate
    structural: bool


def match_entry(entry: dict[str, Any], candidate: Candidate) -> Match | None:
    """Does this stored candidate satisfy the gold entry's match contract?"""
    rule = entry.get("match") or {}
    haystack = candidate.all_tokens

    for forbidden in rule.get("forbid_any") or []:
        if phrase_in(forbidden, haystack):
            return None

    subject_any = rule.get("subject_any") or []
    if subject_any and not any(phrase_in(p, haystack) for p in subject_any):
        return None

    predicate_any = rule.get("predicate_any") or []
    if predicate_any and not any(phrase_in(p, haystack) for p in predicate_any):
        return None

    object_all = rule.get("object_all") or []
    if object_all and not all(phrase_in(p, haystack) for p in object_all):
        return None

    groups = rule.get("object_any_groups") or []
    if groups and not any(all(phrase_in(p, haystack) for p in group) for group in groups):
        return None

    if not (subject_any or predicate_any or object_all or groups):
        return None

    # Structural = the match landed on the candidate's actual subject/object
    # fields, not merely somewhere in the flat fact string. Reported so a run
    # that matches only lexically is visible as the weaker evidence it is.
    structural = True
    if subject_any:
        structural &= any(phrase_in(p, candidate.subject_tokens) for p in subject_any)
    if object_all:
        structural &= all(phrase_in(p, candidate.object_tokens) for p in object_all)
    elif groups:
        structural &= any(all(phrase_in(p, candidate.object_tokens) for p in group) for group in groups)
    return Match(candidate=candidate, structural=structural)


@dataclass
class EntryResult:
    entry: dict[str, Any]
    visible: list[Match]
    hidden: list[Match]

    @property
    def stored_visible(self) -> bool:
        return bool(self.visible)

    @property
    def stored_anywhere(self) -> bool:
        return bool(self.visible or self.hidden)

    @property
    def structural(self) -> bool:
        return any(match.structural for match in self.visible)

    @property
    def hidden_reason(self) -> str:
        if not self.hidden:
            return ""
        return Counter(match.candidate.reason for match in self.hidden).most_common(1)[0][0]


def evaluate(entries: list[dict[str, Any]], candidates: list[Candidate]) -> list[EntryResult]:
    results: list[EntryResult] = []
    for entry in entries:
        visible: list[Match] = []
        hidden: list[Match] = []
        for candidate in candidates:
            match = match_entry(entry, candidate)
            if match is None:
                continue
            (visible if candidate.visible else hidden).append(match)
        results.append(EntryResult(entry=entry, visible=visible, hidden=hidden))
    return results


# ---------------------------------------------------------------------------
# citation verification
# ---------------------------------------------------------------------------


def verify_citations(goldset: dict[str, Any], portal_root: Path) -> int:
    """Every entry must cite a real line containing its verbatim quote.

    This is what stops the gold set from drifting into fiction: if the portal
    moves a line and nobody updates the citation, scoring fails loudly rather
    than measuring against a sentence that no longer exists.
    """
    content_root = portal_root / str(goldset["meta"]["portal_content_root"])
    failures: list[str] = []
    checked = 0
    cache: dict[Path, list[str]] = {}

    for band in ("must_remember", "must_not_remember", "edge_cases"):
        for entry in goldset.get(band) or []:
            checked += 1
            path = content_root / str(entry["file"])
            if path not in cache:
                if not path.exists():
                    failures.append(f"{entry['id']}: missing file {path}")
                    cache[path] = []
                    continue
                cache[path] = path.read_text(encoding="utf-8").splitlines()
            lines = cache[path]
            index = int(entry["line"]) - 1
            if not lines or index < 0 or index >= len(lines):
                failures.append(f"{entry['id']}: line {entry['line']} out of range in {entry['file']}")
                continue
            if str(entry["quote"]) not in lines[index]:
                failures.append(
                    f"{entry['id']}: quote not found at {entry['file']}:{entry['line']}\n"
                    f"      want: {entry['quote']!r}\n"
                    f"      line: {lines[index].strip()!r}"
                )

    print(f"citation check: {checked} entries against {portal_root}")
    if failures:
        print(f"  FAILED ({len(failures)}):")
        for failure in failures:
            print(f"    - {failure}")
        return 1
    print("  all citations verified: every quote is an exact substring of its cited line")
    return 0


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _bar(label: str, value: float, width: int = 28) -> str:
    filled = round(value * width)
    return f"{label:<22} {'#' * filled}{'.' * (width - filled)}  {value * 100:5.1f}%"


def score(
    goldset: dict[str, Any],
    candidates: list[Candidate],
    quarantine_tables: list[str],
    graph_label: str,
    show: str,
) -> dict[str, Any]:
    must = evaluate(goldset.get("must_remember") or [], candidates)
    must_not = evaluate(goldset.get("must_not_remember") or [], candidates)
    edges = evaluate(goldset.get("edge_cases") or [], candidates)

    visible_candidates = [c for c in candidates if c.visible]

    hits = [r for r in must if r.stored_visible]
    quarantined = [r for r in must if not r.stored_visible and r.stored_anywhere]
    missed = [r for r in must if not r.stored_anywhere]

    false_stores = [r for r in must_not if r.stored_visible]
    kept_out = [r for r in must_not if not r.stored_visible]
    kept_out_quarantined = [r for r in kept_out if r.stored_anywhere]
    kept_out_never = [r for r in kept_out if not r.stored_anywhere]

    true_positive = len(hits)
    false_negative = len(quarantined) + len(missed)
    false_positive = len(false_stores)

    recall = _rate(true_positive, true_positive + false_negative)
    precision = _rate(true_positive, true_positive + false_positive)
    f1 = _rate(2 * precision * recall, precision + recall) if (precision + recall) else 0.0
    false_store_rate = _rate(false_positive, len(must_not))

    # Noise: visible facts the gold set does not account for either way.
    gold_matched: set[int] = set()
    for result in must + must_not + edges:
        for match in result.visible:
            gold_matched.add(id(match.candidate))
    noise = [c for c in visible_candidates if id(c) not in gold_matched]
    noise_ratio = _rate(len(noise), len(visible_candidates))

    edge_correct = [
        r
        for r in edges
        if (r.stored_visible and str(r.entry["expected"]) == "keep")
        or (not r.stored_visible and str(r.entry["expected"]) == "drop")
    ]

    line = "=" * 78
    print(line)
    print(f"ACTIONABILITY SCORECARD -- {graph_label}")
    print(f"gold set: {GOLDSET_PATH.name} v{goldset['meta']['version']} (portal @ {goldset['meta']['portal_commit']})")
    print(line)

    print("\n[store]")
    print(f"  fact rows (excl. provenance edges)   {len(candidates)}")
    print(f"  context-visible                      {len(visible_candidates)}")
    print(f"  stored but not context-visible       {len(candidates) - len(visible_candidates)}")
    hidden_reasons = Counter(c.reason for c in candidates if not c.visible)
    if hidden_reasons:
        print(
            f"    by reason                          {', '.join(f'{k} {v}' for k, v in hidden_reasons.most_common())}"
        )
    if quarantine_tables:
        print(f"  quarantine store                     {', '.join(quarantine_tables)}")
    else:
        print(
            "  quarantine store                     none found "
            "(no table matching *quarantin*; quarantine signals read from fact status only)"
        )

    print(f"\n[must-remember]  n={len(must)}")
    print(f"  hit (stored AND context-visible)     {len(hits)}")
    print(f"  quarantined (stored, NOT visible)    {len(quarantined)}   <- gate misjudged it")
    print(f"  miss (never extracted)               {len(missed)}   <- extractor never produced it")
    structural = sum(1 for r in hits if r.structural)
    print(f"  of the hits, structural matches      {structural}/{len(hits)} (subject/object aligned, not just lexical)")

    print(f"\n[must-not-remember]  n={len(must_not)}")
    print(
        f"  correctly kept out                   {len(kept_out)}"
        f"  (never extracted {len(kept_out_never)}, quarantined {len(kept_out_quarantined)})"
    )
    print(f"  FALSE STORE (visible in context)     {len(false_stores)}")

    print(f"\n[edge cases]  n={len(edges)}  (scored separately -- never folded into the headline)")
    print(f"  agreed with the expected call        {len(edge_correct)}/{len(edges)}")
    for result in edges:
        expected = str(result.entry["expected"])
        actual = "keep" if result.stored_visible else "drop"
        flag = "ok " if expected == actual else "DIFF"
        detail = actual if actual == "keep" else (result.hidden_reason or "absent")
        print(f"    [{flag}] {result.entry['id']}  expected {expected:<5} got {detail}")

    print("\n[headline]")
    print("  " + _bar("recall", recall))
    print("  " + _bar("precision", precision))
    print("  " + _bar("F1", f1))
    print("  " + _bar("false-store rate", false_store_rate))
    print("  " + _bar("noise ratio", noise_ratio))
    print(
        f"\n  recall           = hit / must-remember                    "
        f"{true_positive}/{true_positive + false_negative}"
    )
    print(
        f"  precision        = hit / (hit + false stores)             {true_positive}/{true_positive + false_positive}"
    )
    print(f"  false-store rate = false stores / must-not-remember       {false_positive}/{len(must_not)}")
    print(f"  noise ratio      = unaccounted visible facts / visible    {len(noise)}/{len(visible_candidates)}")

    if show in ("misses", "all"):
        print("\n[must-remember failures]")
        for result in quarantined:
            print(f"  QUARANTINED {result.entry['id']}  {result.entry['statement']}")
            print(f"      reason: {result.hidden_reason}; saves: {result.entry['saves']}")
            print(f"      stored as: {result.hidden[0].candidate.fact[:110]}")
        for result in missed:
            print(f"  MISS        {result.entry['id']}  {result.entry['statement']}")
            print(f"      saves: {result.entry['saves']}")
            print(f"      source: {result.entry['file']}:{result.entry['line']}")

    if show in ("false-stores", "all") and false_stores:
        print("\n[false stores]")
        for result in false_stores:
            print(f"  {result.entry['id']}  {result.entry['statement']}")
            print(f"      why not: {result.entry['why_not']}")
            print(f"      stored as: {result.visible[0].candidate.fact[:110]}")

    if show in ("noise", "all") and noise:
        print(f"\n[noise -- {len(noise)} visible facts the gold set does not account for]")
        clusters = Counter(f"{c.subject} | {c.predicate}" for c in noise)
        for cluster, count in clusters.most_common(15):
            print(f"  {count:>3}x  {cluster}")

    return {
        "graph": graph_label,
        "goldset_version": goldset["meta"]["version"],
        "counts": {
            "fact_rows": len(candidates),
            "context_visible": len(visible_candidates),
            "must_remember": len(must),
            "hit": len(hits),
            "quarantined": len(quarantined),
            "miss": len(missed),
            "must_not_remember": len(must_not),
            "false_store": len(false_stores),
            "kept_out": len(kept_out),
            "edge_cases": len(edges),
            "edge_agreed": len(edge_correct),
            "noise": len(noise),
        },
        "metrics": {
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "f1": round(f1, 4),
            "false_store_rate": round(false_store_rate, 4),
            "noise_ratio": round(noise_ratio, 4),
        },
        "misses": [r.entry["id"] for r in missed],
        "quarantined": [r.entry["id"] for r in quarantined],
        "false_stores": [r.entry["id"] for r in false_stores],
    }


def score_temporal_authority(section: dict[str, Any], candidates: list[Candidate], graph_label: str) -> dict[str, Any]:
    """WS-25 T5: score the ``temporal_authority`` section against its own
    fixture graph (see ``build_temporal_authority_fixture.py``) -- NOT the
    portal-KB ``--graph``.  Same match contract, same isolation (no
    ``memotron`` import): ``must_be_current`` entries must land on a
    VISIBLE candidate; ``must_not_be_current`` entries must NOT.
    """
    must_current = evaluate(section.get("must_be_current") or [], candidates)
    must_not_current = evaluate(section.get("must_not_be_current") or [], candidates)

    current_ok = [r for r in must_current if r.stored_visible]
    current_failed = [r for r in must_current if not r.stored_visible]
    not_current_ok = [r for r in must_not_current if not r.stored_visible]
    not_current_failed = [r for r in must_not_current if r.stored_visible]

    total = len(must_current) + len(must_not_current)
    passed = len(current_ok) + len(not_current_ok)
    score_value = _rate(passed, total)

    print("\n" + "=" * 78)
    print(f"TEMPORAL AUTHORITY (WS-25 T5) -- {graph_label}")
    print("=" * 78)
    for result in must_current:
        flag = "ok  " if result.stored_visible else "FAIL"
        detail = "current" if result.stored_visible else (result.hidden_reason or "absent")
        print(f"  [{flag}] {result.entry['id']}  expected current, got {detail}")
    for result in must_not_current:
        flag = "ok  " if not result.stored_visible else "FAIL"
        detail = "current" if result.stored_visible else (result.hidden_reason or "absent")
        print(f"  [{flag}] {result.entry['id']}  expected NOT current, got {detail}")
    print(f"\n  score: {passed}/{total} = {score_value * 100:.1f}%")

    return {
        "graph": graph_label,
        "score": round(score_value, 4),
        "passed": passed,
        "total": total,
        "failed_ids": [r.entry["id"] for r in (current_failed + not_current_failed)],
    }


def score_compaction_survival(section: dict[str, Any], candidates: list[Candidate], graph_label: str) -> dict[str, Any]:
    """WS-28 T3 (+T1 companion): score the ``compaction_survival`` section
    against its own fixture graph (see
    ``build_compaction_survival_fixture.py``) -- NOT the portal-KB
    ``--graph``.  Same match contract and isolation (no ``memotron``
    import) as :func:`score_temporal_authority`: ``must_be_current`` entries
    must land on a VISIBLE candidate; ``must_not_be_current`` entries must
    NOT (including "never stored at all", which is the correct outcome for
    the below-threshold-command case).
    """
    must_current = evaluate(section.get("must_be_current") or [], candidates)
    must_not_current = evaluate(section.get("must_not_be_current") or [], candidates)

    current_ok = [r for r in must_current if r.stored_visible]
    current_failed = [r for r in must_current if not r.stored_visible]
    not_current_ok = [r for r in must_not_current if not r.stored_visible]
    not_current_failed = [r for r in must_not_current if r.stored_visible]

    total = len(must_current) + len(must_not_current)
    passed = len(current_ok) + len(not_current_ok)
    score_value = _rate(passed, total)

    print("\n" + "=" * 78)
    print(f"COMPACTION SURVIVAL (WS-28 T3) -- {graph_label}")
    print("=" * 78)
    for result in must_current:
        flag = "ok  " if result.stored_visible else "FAIL"
        detail = "current" if result.stored_visible else (result.hidden_reason or "absent")
        print(f"  [{flag}] {result.entry['id']}  expected current, got {detail}")
    for result in must_not_current:
        flag = "ok  " if not result.stored_visible else "FAIL"
        detail = "current" if result.stored_visible else (result.hidden_reason or "absent")
        print(f"  [{flag}] {result.entry['id']}  expected NOT current, got {detail}")
    print(f"\n  score: {passed}/{total} = {score_value * 100:.1f}%")

    return {
        "graph": graph_label,
        "score": round(score_value, 4),
        "passed": passed,
        "total": total,
        "failed_ids": [r.entry["id"] for r in (current_failed + not_current_failed)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--graph",
        help="graph name under ingest/.memotron/ or an explicit .sqlite path",
    )
    parser.add_argument(
        "--temporal-authority-graph",
        help=(
            "WS-25 T5: graph name under ingest/.memotron/ or an explicit .sqlite "
            "path to score the goldset's temporal_authority section against "
            "(default: ingest/.memotron/temporal_authority_fixture.sqlite, the "
            "fixture build_temporal_authority_fixture.py writes -- scored only if "
            "that path exists, since it is a gitignored build artifact)"
        ),
    )
    parser.add_argument(
        "--compaction-survival-graph",
        help=(
            "WS-28 T3: graph name under ingest/.memotron/ or an explicit .sqlite "
            "path to score the goldset's compaction_survival section against "
            "(default: ingest/.memotron/compaction_survival_fixture.sqlite, the "
            "fixture build_compaction_survival_fixture.py writes -- scored only if "
            "that path exists, since it is a gitignored build artifact)"
        ),
    )
    parser.add_argument(
        "--verify-citations",
        action="store_true",
        help="check every gold-set quote against the portal working tree and exit",
    )
    parser.add_argument("--portal-root", type=Path, default=DEFAULT_PORTAL_ROOT)
    parser.add_argument(
        "--show",
        choices=["none", "misses", "false-stores", "noise", "all"],
        default="misses",
        help="which failure detail to print (default: misses)",
    )
    parser.add_argument("--json", type=Path, help="also write the scorecard as JSON")
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--max-false-store-rate", type=float, default=0.10)
    parser.add_argument("--max-noise-ratio", type=float, default=0.35)
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="report only; do not exit non-zero when a threshold is missed",
    )
    args = parser.parse_args(argv)

    goldset = yaml.safe_load(GOLDSET_PATH.read_text(encoding="utf-8"))

    if args.verify_citations:
        return verify_citations(goldset, args.portal_root)

    # WS-25 T5: the temporal_authority lane is available whenever an explicit
    # --temporal-authority-graph was passed, OR the gold set carries the
    # section AND its default fixture (a gitignored build artifact) has
    # already been built -- either makes --graph optional.
    ta_graph_value = args.temporal_authority_graph or str(GRAPH_DIR / "temporal_authority_fixture.sqlite")
    ta_default_path = Path(ta_graph_value).expanduser()
    ta_available = "temporal_authority" in goldset and (args.temporal_authority_graph or ta_default_path.exists())

    # WS-28 T3: same "available" contract as temporal_authority above --
    # either an explicit --compaction-survival-graph, or the default
    # gitignored fixture already built.
    cs_graph_value = args.compaction_survival_graph or str(GRAPH_DIR / "compaction_survival_fixture.sqlite")
    cs_default_path = Path(cs_graph_value).expanduser()
    cs_available = "compaction_survival" in goldset and (args.compaction_survival_graph or cs_default_path.exists())

    if not args.graph and not ta_available and not cs_available:
        parser.error(
            "--graph, --temporal-authority-graph, or --compaction-survival-graph "
            "is required (or use --verify-citations)"
        )

    report: dict[str, Any] | None = None
    if args.graph:
        path = resolve_graph(args.graph)
        candidates = load_graph_facts(path)
        quarantined, quarantine_tables = load_quarantine(path)
        candidates.extend(quarantined)
        report = score(goldset, candidates, quarantine_tables, path.name, args.show)

    # Scored against its OWN small fixture graph, never the portal-KB graph
    # above.  Silent no-op when the section is absent from the gold set or
    # its default fixture has not been built.
    ta_report: dict[str, Any] | None = None
    if "temporal_authority" in goldset:
        if ta_available:
            ta_path = resolve_graph(ta_graph_value)
            ta_candidates = load_graph_facts(ta_path)
            ta_report = score_temporal_authority(goldset["temporal_authority"], ta_candidates, ta_path.name)
        elif args.graph:
            # --graph alone was enough to satisfy the "required" gate above;
            # note that temporal_authority was skipped rather than silently
            # omitting it from the report.
            print(
                f"\n(temporal_authority: no fixture at {ta_default_path}; run "
                "`uv run ingest/build_temporal_authority_fixture.py` to build it)"
            )

    # Scored against its OWN small fixture graph, never the portal-KB graph
    # above.  Silent no-op when the section is absent from the gold set or
    # its default fixture has not been built.
    cs_report: dict[str, Any] | None = None
    if "compaction_survival" in goldset:
        if cs_available:
            cs_path = resolve_graph(cs_graph_value)
            cs_candidates = load_graph_facts(cs_path)
            cs_report = score_compaction_survival(goldset["compaction_survival"], cs_candidates, cs_path.name)
        elif args.graph:
            print(
                f"\n(compaction_survival: no fixture at {cs_default_path}; run "
                "`uv run ingest/build_compaction_survival_fixture.py` to build it)"
            )

    combined_report: dict[str, Any] = dict(report) if report is not None else {}
    if ta_report is not None:
        combined_report["temporal_authority"] = ta_report
    if cs_report is not None:
        combined_report["compaction_survival"] = cs_report

    if args.json:
        args.json.write_text(json.dumps(combined_report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")

    failures = []
    if report is not None:
        metrics = report["metrics"]
        if metrics["recall"] < args.min_recall:
            failures.append(f"recall {metrics['recall']:.2f} < {args.min_recall:.2f}")
        if metrics["false_store_rate"] > args.max_false_store_rate:
            failures.append(f"false-store rate {metrics['false_store_rate']:.2f} > {args.max_false_store_rate:.2f}")
        if metrics["noise_ratio"] > args.max_noise_ratio:
            failures.append(f"noise ratio {metrics['noise_ratio']:.2f} > {args.max_noise_ratio:.2f}")
    if ta_report is not None and ta_report["score"] < 1.0:
        failures.append(
            f"temporal_authority score {ta_report['score']:.2f} < 1.00 (failed: {', '.join(ta_report['failed_ids'])})"
        )
    if cs_report is not None and cs_report["score"] < 1.0:
        failures.append(
            f"compaction_survival score {cs_report['score']:.2f} < 1.00 (failed: {', '.join(cs_report['failed_ids'])})"
        )

    print("\n" + "=" * 78)
    if failures:
        print("GATE: REJECT")
        for failure in failures:
            print(f"  - {failure}")
        print("=" * 78)
        return 0 if args.no_gate else 1
    print("GATE: ACCEPT -- all scored thresholds within bound")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
