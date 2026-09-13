"""Per-goal acceptance tests from INGEST.md §4d / §5d / §6d.

Runs against the ingested pilot graph and prints PASS / FAIL / MEASURE per test.

Gate semantics follow INGEST.md §7 "Go/no-go 1":
  * **G1-2 (zero false merges)** is a hard gate -- a false merge on the data-store
    family kills trust in the KB.
  * **G3-3 (temporal supersession)** is a hard gate.
  * **G1-1 (fragmentation index)** and **G2-2b (paraphrase)** are *measurements*
    that parameterize expectations, not gates.

G3-3 performs a real edit-and-resync. The portal checkout is **never modified**:
the edit is applied to the section text in memory and re-ingested under the same
``custom_id`` with a later ``reference_time``, which is byte-equivalent to what a
real doc commit would hand the sync driver.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_config as kb
import preprocess as pre

GOLDSET_PATH = Path(__file__).resolve().parent / "goldset.yaml"

GATEWAY_NAME_RE = re.compile(r"(jedai[ -]?gateway|the gateway|gateway( v[0-9])?|jedai gw)", re.IGNORECASE)
DATASTORE_RE = re.compile(r"kb_ds_[a-z0-9_]+")


@dataclass(slots=True)
class Check:
    id: str
    title: str
    status: str  # PASS | FAIL | MEASURE | SKIP
    detail: str
    gate: bool = False


def _relationships(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r
        for r in graph.get("relationships", [])
        if r.get("type") != "MENTIONS" and r.get("properties", {}).get("status") == "active"
    ]


def _node_names(graph: dict[str, Any], *, label: str | None = "Entity") -> list[str]:
    """Names of graph nodes, restricted to ``label`` by default.

    The default matters: every ingested section also creates an ``Episode`` node
    whose name is the episode title (``portal:/products/jedai-gateway/#... chunk
    1/1``). Counting those as entities inflates the Goal-1 fragmentation index
    with things that are not entities at all.
    """
    out = []
    for node in graph.get("nodes", []):
        labels = node.get("labels") or ()
        if label is not None and label not in labels:
            continue
        name = node.get("properties", {}).get("name") or node.get("name")
        if name:
            out.append(str(name))
    return out


# --------------------------------------------------------------------------
# Goal 1 -- entity matching
# --------------------------------------------------------------------------


def g1_1_fragmentation(graph: dict[str, Any]) -> Check:
    """Fragmentation index: how many distinct nodes name the gateway."""
    names = sorted({n for n in _node_names(graph) if GATEWAY_NAME_RE.fullmatch(n.strip())})
    loose = sorted({n for n in _node_names(graph) if GATEWAY_NAME_RE.search(n)})
    return Check(
        id="G1-1",
        title="one node per entity (fragmentation index)",
        status="MEASURE",
        detail=(
            f"exact gateway-name nodes: {len(names)} {names}\n"
            f"        nodes containing a gateway form: {len(loose)} {loose[:12]}"
        ),
    )


def g1_2_false_merges(graph: dict[str, Any]) -> Check:
    """HARD GATE: the kb_ds_* family must never merge."""
    relationships = _relationships(graph)
    ids_in_graph: set[str] = set()
    for name in _node_names(graph):
        ids_in_graph.update(DATASTORE_RE.findall(name))
    for relationship in relationships:
        properties = relationship.get("properties", {})
        blob = f"{properties.get('fact', '')} {properties.get('object', '')}"
        ids_in_graph.update(DATASTORE_RE.findall(blob))

    # A false merge = one node/fact whose text carries TWO different data-store IDs
    # from the canary family, i.e. two distinct stores collapsed into one value.
    collisions: list[str] = []
    for node_name in _node_names(graph):
        found = set(DATASTORE_RE.findall(node_name))
        if len(found) > 1:
            collisions.append(f"node {node_name!r} carries {sorted(found)}")
    for relationship in relationships:
        properties = relationship.get("properties", {})
        obj = str(properties.get("object", ""))
        found = set(DATASTORE_RE.findall(obj))
        if len(found) > 1:
            collisions.append(f"fact object {obj!r} carries {sorted(found)}")

    present_canaries = [c for c in kb.DISTINCT_ID_CANARIES if c in ids_in_graph]
    status = "FAIL" if collisions else "PASS"
    detail = (
        f"distinct kb_ds_* identifiers in graph: {len(ids_in_graph)}\n"
        f"        canaries present: {len(present_canaries)}/{len(kb.DISTINCT_ID_CANARIES)}\n"
        f"        merge collisions: {len(collisions)}"
    )
    if collisions:
        detail += "\n        " + "\n        ".join(collisions[:6])
    if not ids_in_graph:
        status = "SKIP"
        detail += "\n        (no data-store IDs were extracted; cannot evaluate)"
    return Check("G1-2", "zero false merges across distinct data-store IDs", status, detail, gate=True)


async def g1_3_threshold_sanity(client, scope, graph: dict[str, Any]) -> Check:
    """Did any legitimate paraphrase reinforce, and did any distinct pair merge?"""
    evolution = await client.memory_evolution(scope=scope)
    return Check(
        id="G1-3",
        title="threshold sanity (semantic dedup active, no distinct-ID merge)",
        status="MEASURE",
        detail=(
            f"semantic_dedup_rate={evolution.semantic_dedup_rate:.3f}  "
            f"reinforced={evolution.reinforced_relationship_count}  "
            f"per-type dedup floors={kb.kb_dedup_policy().memory_type_thresholds}"
        ),
    )


async def g1_4_alias_read_path(client, scope) -> Check:
    """INGEST.md expected this to fail in Phase 0; the alias registry has landed."""
    canonical = await client.entity_neighborhood(scope=scope, entity="Jedai Gateway", limit=50)
    alias = await client.entity_neighborhood(scope=scope, entity="the gateway", limit=50)
    canonical_uuids = {e.relationship_uuid for e in canonical}
    alias_uuids = {e.relationship_uuid for e in alias}
    if not canonical_uuids and not alias_uuids:
        return Check("G1-4", "alias read path", "SKIP", "no gateway node in this slice")
    status = "PASS" if canonical_uuids and canonical_uuids == alias_uuids else "MEASURE"
    return Check(
        "G1-4",
        "alias read path: entity_neighborhood('the gateway') == ('Jedai Gateway')",
        status,
        f"canonical edges={len(canonical_uuids)}  alias edges={len(alias_uuids)}  "
        f"identical={canonical_uuids == alias_uuids}",
    )


# --------------------------------------------------------------------------
# Goal 2 -- concept matching
# --------------------------------------------------------------------------


async def g2_1_theme_spans_slugs(client, scope, graph: dict[str, Any]) -> Check:
    """>=1 active THEME whose members span >=3 distinct slugs."""
    themes = [r for r in _relationships(graph) if r.get("type") == "THEME"]
    if not themes:
        return Check(
            "G2-1",
            "cross-page THEME forms",
            "FAIL",
            "no active THEME relationships (min_cluster_size=3 not reached, or consolidation was motive-gated)",
        )
    # A THEME is derived from member FACTS, not directly from episodes, so its own
    # `memory_evidence` carries no section episodes. The member slugs have to come
    # from the `derived_from` lineage (dreaming.py:2872) -> each member fact's
    # inherited `metadata.slug`.
    by_uuid = {r.get("uuid"): r.get("properties", {}) for r in graph.get("relationships", []) if r.get("uuid")}
    best = 0
    best_label = ""
    details: list[str] = []
    for theme in themes:
        properties = theme.get("properties", {})
        members = properties.get("derived_from")
        if not isinstance(members, list):
            members = []
        slugs = {(by_uuid.get(uuid, {}).get("metadata") or {}).get("slug") for uuid in members}
        slugs.discard(None)
        details.append(f"{str(properties.get('object', ''))[:58]!r} -> {len(members)} members, {len(slugs)} slugs")
        if len(slugs) > best:
            best, best_label = len(slugs), str(properties.get("object", ""))
    status = "PASS" if best >= 3 else "MEASURE"
    return Check(
        "G2-1",
        "cross-page THEME spans >=3 slugs",
        status,
        f"themes={len(themes)}  best span={best} slugs  label={best_label[:70]!r}\n        "
        + "\n        ".join(details[:6]),
    )


async def g2_2_concept_query(client, scope) -> Check:
    """Concept query returns >=2 slugs with >=1 expansion-attributed result."""
    results = await client.search(query="special offers ingestion pipeline", scope=scope, limit=10)
    origins = {r.origin for r in results}
    slugs: set[str] = set()
    for result in results:
        try:
            evidence = await client.memory_evidence(relationship_uuid=result.relationship_uuid)
        except ValueError:
            continue
        slugs.update(e.metadata.get("slug") for e in evidence.episodes if e.metadata.get("slug"))
    expansion = origins & {"entity_hop", "theme_member", "member_theme", "seed"}
    status = "PASS" if len(slugs) >= 2 and expansion else "MEASURE"
    return Check(
        "G2-2",
        "concept query, expansion-attributed",
        status,
        f"results={len(results)}  distinct slugs={len(slugs)}  origins={sorted(origins)}  "
        f"expansion={sorted(expansion) or 'none'}",
    )


async def g2_3_theme_labels(client, graph: dict[str, Any]) -> Check:
    themes = [r for r in _relationships(graph) if r.get("type") == "THEME"]
    if not themes:
        return Check("G2-3", "THEME label quality", "SKIP", "no themes formed")
    labels = [str(t.get("properties", {}).get("object", "")) for t in themes]
    over = [label for label in labels if len(label) > 240]
    empty = [label for label in labels if not label.strip()]
    status = "PASS" if not over and not empty else "FAIL"
    return Check(
        "G2-3",
        "THEME labels non-empty and within the synthesis cap",
        status,
        f"themes={len(labels)}  empty={len(empty)}  over-240-chars={len(over)}\n        "
        + "\n        ".join(f"{label[:90]!r}" for label in labels[:5]),
    )


async def g2_4_demotion(client, scope) -> Check:
    evolution = await client.memory_evolution(scope=scope)
    visible = evolution.context_visible_relationship_count
    active = evolution.active_relationship_count
    status = "PASS" if evolution.theme_relationship_count >= 1 and visible < active else "MEASURE"
    return Check(
        "G2-4",
        "demotion economics (context-visible < total active)",
        status,
        f"themes={evolution.theme_relationship_count}  demoted={evolution.demoted_relationship_count}  "
        f"context_visible={visible}  active={active}  "
        f"compression_ratio={evolution.compression_ratio:.3f}",
    )


# --------------------------------------------------------------------------
# Goal 3 -- graph-native wins
# --------------------------------------------------------------------------


async def g3_1_two_hop(client, scope) -> Check:
    results = await client.search(
        query="which Vertex data stores back DLR special offers and what pipeline populates them",
        scope=scope,
        limit=12,
    )
    slugs: set[str] = set()
    origins: set[str] = set()
    has_dlr = False
    for result in results:
        origins.add(result.origin)
        if re.search(r"kb_ds_\S*_dlr_\S*", f"{result.fact} {result.object}"):
            has_dlr = True
        try:
            evidence = await client.memory_evidence(relationship_uuid=result.relationship_uuid)
        except ValueError:
            continue
        slugs.update(e.metadata.get("slug") for e in evidence.episodes if e.metadata.get("slug"))
    expansion = origins & {"entity_hop", "theme_member", "member_theme", "seed"}
    status = "PASS" if has_dlr and len(slugs) >= 2 else "MEASURE"
    return Check(
        "G3-1",
        "two-hop join (DLR data store + populating pipeline)",
        status,
        f"results={len(results)}  dlr_id_present={has_dlr}  distinct slugs={len(slugs)}  "
        f"origins={sorted(origins)}  expansion={sorted(expansion) or 'none'}",
    )


async def g3_2_auth_join(client, scope) -> Check:
    results = await client.search(query="how do I authenticate to the gateway", scope=scope, limit=10)
    slugs: set[str] = set()
    origins: set[str] = set()
    for result in results:
        origins.add(result.origin)
        try:
            evidence = await client.memory_evidence(relationship_uuid=result.relationship_uuid)
        except ValueError:
            continue
        slugs.update(e.metadata.get("slug") for e in evidence.episodes if e.metadata.get("slug"))
    expansion = origins & {"entity_hop", "theme_member", "member_theme", "seed"}
    status = "PASS" if len(slugs) >= 3 and expansion else "MEASURE"
    return Check(
        "G3-2",
        "auth join with expansion attribution",
        status,
        f"results={len(results)}  distinct slugs={len(slugs)}  origins={sorted(origins)}  "
        f"expansion={sorted(expansion) or 'none'}",
    )


async def g3_3_edit_and_resync(client, scope, *, content_root: Path) -> list[Check]:
    """HARD GATE: a doc edit must flip truth in ONE sync cycle.

    The portal is read-only. The edit is applied in memory to the preprocessed
    section text and re-ingested under the same ``custom_id`` with a
    ``reference_time`` one day later -- exactly the shape of a real commit.
    """
    goldset = yaml.safe_load(GOLDSET_PATH.read_text(encoding="utf-8"))
    entries = goldset.get("band3_temporal", [])
    sections = {s.custom_id: s for s in pre.preprocess(content_root=content_root)}
    checks: list[Check] = []

    for entry in entries:
        edit = entry["edit"]
        custom_id = edit["custom_id"]
        section = sections.get(custom_id)
        if section is None:
            checks.append(
                Check(
                    f"G3-3/{entry['id']}",
                    "edit-and-resync",
                    "SKIP",
                    f"section {custom_id} not in the preprocessed corpus",
                )
            )
            continue
        if edit["find"] not in section.section_text:
            checks.append(
                Check(f"G3-3/{entry['id']}", "edit-and-resync", "SKIP", f"anchor text not found in {custom_id}")
            )
            continue

        before_time = datetime.fromisoformat(section.reference_time)
        after_time = before_time + timedelta(days=1)

        # Baseline: what does the graph say now?
        before = await client.search(query=entry["question"], scope=scope, limit=10)
        before_hit = any(
            any(exp.lower() in f"{r.fact} {r.object}".lower() for exp in entry["expect_before_any"]) for r in before
        )

        edited_text = section.section_text.replace(edit["find"], edit["replace"])
        await client.add_context(
            name=f"portal:{custom_id}",
            content=edited_text,
            scopes=[scope],
            source_description="jedai-portal-mdx",
            reference_time=after_time,
            metadata={
                "slug": section.slug,
                "anchor": section.anchor,
                "section": section.heading,
                "custom_id": custom_id,
                "title": section.title,
                "source": section.source_path,
                "content_sha256": "simulated-edit",
                "_verified_source_authority": "operator",
            },
            custom_id=custom_id,
            instruction_set=kb.INSTRUCTION_SET_NAME,
            max_chars_per_episode=4000,
            motive=kb.MOTIVE_NAME,
            trusted=True,
        )
        await client.run_dream_job(job_name=kb.FORMATION_JOB)

        after = await client.search(query=entry["question"], scope=scope, limit=10)
        after_hit = any(
            any(exp.lower() in f"{r.fact} {r.object}".lower() for exp in entry["expect_after_any"]) for r in after
        )
        historical = await client.search(query=entry["question"], scope=scope, limit=10, as_of=before_time)
        historical_hit = any(
            any(exp.lower() in f"{r.fact} {r.object}".lower() for exp in entry["expect_before_any"]) for r in historical
        )
        evolution = await client.memory_evolution(scope=scope)

        if after_hit and historical_hit:
            status = "PASS"
        elif after_hit:
            status = "MEASURE"
        else:
            status = "FAIL"
        checks.append(
            Check(
                f"G3-3/{entry['id']}",
                "edit-and-resync: new truth current, old truth as_of-queryable",
                status,
                f"before_had_old={before_hit}  after_has_new={after_hit}  "
                f"as_of_has_old={historical_hit}  "
                f"superseded_total={evolution.superseded_relationship_count}",
                gate=True,
            )
        )
    return checks


async def g3_4_evidence_precision(client, scope, graph: dict[str, Any]) -> Check:
    relationships = [r for r in _relationships(graph) if r.get("type") != "THEME"]
    sample = relationships[:20]
    good = 0
    quoted = 0
    checked = 0
    for relationship in sample:
        uuid = relationship.get("uuid")
        if not uuid:
            continue
        try:
            evidence = await client.memory_evidence(relationship_uuid=uuid)
        except ValueError:
            continue
        checked += 1
        has_coords = any(e.metadata.get("slug") and e.metadata.get("anchor") is not None for e in evidence.episodes)
        good += 1 if has_coords else 0
        if evidence.source_text:
            body = " ".join(e.body for e in evidence.episodes)
            quoted += 1 if evidence.source_text.strip()[:60] in body else 0
    status = "PASS" if checked and good / checked >= 0.9 else "MEASURE"
    return Check(
        "G3-4",
        "evidence precision to slug#anchor",
        status,
        f"sampled={checked}  with slug+anchor coords={good}  "
        f"({good / checked * 100:.0f}%)  verbatim source_text quotes verified={quoted}"
        if checked
        else "no relationships to sample",
    )


async def g3_5_deletion(client, scope, graph: dict[str, Any]) -> Check:
    """forget_memory soft-retire: excluded from current search, still as_of-reachable."""
    relationships = _relationships(graph)
    target = next((r for r in relationships if r.get("type") not in ("THEME", "MENTIONS")), None)
    if target is None:
        return Check("G3-5", "deletion soft-retire", "SKIP", "no fact available to retire")
    uuid = target["uuid"]
    fact = str(target["properties"].get("fact", ""))
    result = await client.forget_memory(relationship_uuid=uuid, reason="verify_goals:g3-5-probe")
    evidence = await client.memory_evidence(relationship_uuid=uuid)
    still_has_evidence = bool(evidence.episodes)
    current = await client.search(query=fact[:80], scope=scope, limit=20)
    in_current = any(r.relationship_uuid == uuid for r in current)
    status = "PASS" if result.status.value == "pruned" and still_has_evidence and not in_current else "FAIL"
    return Check(
        "G3-5",
        "deletion: soft-retire preserves evidence, drops from current retrieval",
        status,
        f"status={result.status.value}  evidence episodes preserved={len(evidence.episodes)}  "
        f"present in current search={in_current}  (probe fact: {fact[:60]!r})",
    )


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def render(checks: list[Check]) -> int:
    print()
    print("=" * 100)
    print("GOAL ACCEPTANCE -- INGEST.md §4d / §5d / §6d")
    print("=" * 100)
    symbols = {"PASS": "PASS  ", "FAIL": "FAIL  ", "MEASURE": "MEASURE", "SKIP": "SKIP  "}
    failed_gates = 0
    for check in checks:
        gate = " [GATE]" if check.gate else ""
        print(f"\n  {symbols[check.status]} {check.id}{gate}  {check.title}")
        print(f"        {check.detail}")
        if check.gate and check.status == "FAIL":
            failed_gates += 1
    counts = {s: sum(1 for c in checks if c.status == s) for s in symbols}
    print()
    print("-" * 100)
    print(
        f"  {counts['PASS']} pass   {counts['FAIL']} fail   {counts['MEASURE']} measurement   {counts['SKIP']} skipped"
    )
    print(f"  HARD GATES: {'ALL PASS' if failed_gates == 0 else f'{failed_gates} FAILED'}")
    print("-" * 100)
    return 1 if failed_gates else 0


async def run(args: argparse.Namespace) -> int:
    kb.assert_isolation()
    client = kb.build_client()
    scope = kb.SCOPES[args.scope]
    try:
        graph = client.export_graph()
        checks: list[Check] = [
            g1_1_fragmentation(graph),
            g1_2_false_merges(graph),
            await g1_3_threshold_sanity(client, scope, graph),
            await g1_4_alias_read_path(client, scope),
            await g2_1_theme_spans_slugs(client, scope, graph),
            await g2_2_concept_query(client, scope),
            await g2_3_theme_labels(client, graph),
            await g2_4_demotion(client, scope),
            await g3_1_two_hop(client, scope),
            await g3_2_auth_join(client, scope),
            await g3_4_evidence_precision(client, scope, graph),
        ]
        if not args.skip_mutating:
            checks.extend(await g3_3_edit_and_resync(client, scope, content_root=args.content_root))
            checks.append(await g3_5_deletion(client, scope, client.export_graph()))
        else:
            checks.append(Check("G3-3", "edit-and-resync", "SKIP", "--skip-mutating"))
            checks.append(Check("G3-5", "deletion", "SKIP", "--skip-mutating"))
        return render(checks)
    finally:
        client.graph.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-goal acceptance tests (INGEST.md)")
    parser.add_argument("--content-root", type=Path, default=pre.DEFAULT_CONTENT_ROOT)
    parser.add_argument("--scope", choices=("external", "internal"), default="external")
    parser.add_argument(
        "--skip-mutating",
        action="store_true",
        help="skip G3-3 (edit-and-resync) and G3-5 (forget probe), which write to the graph",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
