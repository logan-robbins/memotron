"""Incremental portal -> Memotron sync driver (INGEST.md §2, Phase 0).

    preprocess -> diff against manifest -> add_context (new/changed only) -> dream -> report

The manifest (``ingest/.manifest.json``) maps ``slug#anchor -> content_sha256``.
An unchanged section costs nothing: no episode, no LLM call. A changed section
re-ingests under the same ``custom_id`` so its facts reinforce or supersede in
place. A deleted section is retired via ``forget_memory`` (soft retire: evidence
and ``as_of`` history survive).

Every LLM path here is real: extraction, dream-agent decisions, and rollup
synthesis go to the JedAI Gateway, and vectors come from ``text-embedding-3``.
There is no hermetic fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb_config as kb
import preprocess as pre

from memotron.models import MemoryScope
from memotron.receipts import ReceiptDecisionType

# --------------------------------------------------------------------------
# Cost model (INGEST.md §7 estimates $2-3 for the 77-file pilot)
# --------------------------------------------------------------------------

#: Published claude-haiku-4-5 rates, USD per million tokens. Used only for the
#: --dry-run estimate and the post-run projection; the gateway is the billing
#: authority. Override with --input-rate / --output-rate.
DEFAULT_INPUT_RATE = 1.00
DEFAULT_OUTPUT_RATE = 5.00

#: Measured prompt overhead: instruction contract + profile + graph context, in
#: characters, before the episode body is appended.
PROMPT_OVERHEAD_CHARS = 6000
CHARS_PER_TOKEN = 4.0
#: Extraction responses are JSON memory lists; ~12 memories x ~55 tokens + envelope.
EST_OUTPUT_TOKENS_PER_EPISODE = 750
#: One embedding call per fact + per episode; text-embedding-3 is ~$0.13/MTok but
#: is billed on input only and is negligible next to extraction.
EST_EMBED_RATE = 0.13


@dataclass(slots=True)
class SyncPlan:
    new: list[pre.Section] = field(default_factory=list)
    changed: list[pre.Section] = field(default_factory=list)
    unchanged: list[pre.Section] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def to_ingest(self) -> list[pre.Section]:
        return [*self.new, *self.changed]


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"manifest at {path} is corrupt ({exc}); re-run with --reset") from exc
    return data.get("sections", {})


def save_manifest(path: Path, sections: list[pre.Section], scopes: dict[str, str]) -> None:
    payload = {
        "schema": 1,
        "written_at": datetime.now().astimezone().isoformat(),
        "tenant_id": kb.TENANT_ID,
        "sections": {
            section.custom_id: {
                "content_sha256": section.content_sha256,
                "slug": section.slug,
                "anchor": section.anchor,
                "reference_time": section.reference_time,
                "chars": section.chars,
                "scope_key": scopes.get(section.custom_id, ""),
            }
            for section in sections
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def build_plan(
    sections: list[pre.Section],
    manifest: dict[str, dict[str, Any]],
    *,
    detect_deletions: bool = True,
) -> SyncPlan:
    """Diff preprocessed sections against the manifest.

    ``detect_deletions`` MUST be False on any filtered run. A manifest key is
    "deleted" only if the section is absent from a scan that could have seen it;
    on a run narrowed by ``--only`` / ``--subtree`` / ``--scope`` every section
    outside the filter is missing *by construction*, not because it was removed
    from the portal.

    This is not hypothetical. During this pilot a targeted
    ``--only platform-decision-log/approved.mdx`` re-ingest was run against a full
    manifest, and the unscoped diff classified all 23 other sections as deleted
    and soft-retired **125 facts** from five untouched pages. The retirement
    itself worked exactly as designed -- it was the diff that was wrong.
    """
    plan = SyncPlan()
    seen: set[str] = set()
    for section in sections:
        seen.add(section.custom_id)
        prior = manifest.get(section.custom_id)
        if prior is None:
            plan.new.append(section)
        elif prior.get("content_sha256") != section.content_sha256:
            plan.changed.append(section)
        else:
            plan.unchanged.append(section)
    plan.deleted = sorted(set(manifest) - seen) if detect_deletions else []
    return plan


# --------------------------------------------------------------------------
# Cost estimation
# --------------------------------------------------------------------------


def estimate_cost(sections: list[pre.Section], *, input_rate: float, output_rate: float) -> dict[str, float]:
    episodes = sum(max(1, -(-section.chars // 4000)) for section in sections)
    body_chars = sum(section.chars for section in sections)
    input_tokens = (body_chars + PROMPT_OVERHEAD_CHARS * episodes) / CHARS_PER_TOKEN
    output_tokens = EST_OUTPUT_TOKENS_PER_EPISODE * episodes
    extraction = input_tokens / 1e6 * input_rate + output_tokens / 1e6 * output_rate
    embed = (body_chars / CHARS_PER_TOKEN) * 2 / 1e6 * EST_EMBED_RATE
    return {
        "episodes": float(episodes),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "extraction_usd": extraction,
        "embedding_usd": embed,
        "total_usd": extraction + embed,
    }


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


async def ingest_sections(client, sections: list[pre.Section], *, verbose: bool = True) -> dict[str, Any]:
    """``add_context`` one episode per section, stamped per INGEST.md §2."""
    episode_uuids: list[str] = []
    scope_keys: dict[str, str] = {}
    chunks = 0
    for index, section in enumerate(sections, start=1):
        scope = kb.scope_for(section.frontmatter)
        scope_keys[section.custom_id] = scope.key
        frontmatter = section.frontmatter
        metadata: dict[str, Any] = {
            # Evidence coordinates -- inherited onto every materialized fact
            # ({**episode.metadata, **memory.metadata}), which is what makes
            # fact -> slug#anchor evidence free (INGEST.md §6a).
            "slug": section.slug,
            "anchor": section.anchor,
            "section": section.heading,
            "custom_id": section.custom_id,
            "title": section.title,
            "source": section.source_path,
            "content_sha256": section.content_sha256,
            "heading_level": section.heading_level,
            "internal_links": section.internal_links,
            # The docs portal is operator-authored ground truth.
            "_verified_source_authority": "operator",
        }
        for key in ("description", "tags", "audience", "authorship", "visibility"):
            value = frontmatter.get(key)
            if value not in (None, "", [], {}):
                metadata[key] = value

        result = await client.add_context(
            name=f"portal:{section.custom_id}",
            content=section.section_text,
            scopes=[scope],
            source_description="jedai-portal-mdx",
            reference_time=datetime.fromisoformat(section.reference_time),
            metadata=metadata,
            custom_id=section.custom_id,
            instruction_set=kb.INSTRUCTION_SET_NAME,
            max_chars_per_episode=4000,
            motive=kb.MOTIVE_NAME,
            trusted=True,
        )
        episode_uuids.extend(result.episode_uuids)
        chunks += result.chunks_created
        if verbose:
            print(
                f"  [{index:>3}/{len(sections)}] {section.custom_id[:66]:<66} "
                f"{section.chars:>6}c -> {result.chunks_created} ep  [{scope.scope_id}]"
            )
    return {"episode_uuids": episode_uuids, "chunks": chunks, "scope_keys": scope_keys}


# --------------------------------------------------------------------------
# Deletion retirement
# --------------------------------------------------------------------------


async def retire_deleted(client, deleted_ids: list[str], scopes: list[MemoryScope], *, apply: bool) -> dict[str, Any]:
    """Soft-retire facts whose evidence comes ONLY from deleted sections.

    INGEST.md §2 specifies ``forget_memory`` per fact for a deleted section,
    enumerated via the inherited ``metadata.slug`` / ``shadow_custom_id`` on
    relationship properties.

    The safety guard INGEST.md does not state, but which is required for
    correctness: a fact stated on N pages reinforces to ONE row whose
    ``episode_uuids`` spans all N. Retiring that row because one of its pages was
    deleted would silently drop a fact the surviving pages still assert. So a row
    is retired only when **every** episode backing it belongs to a deleted
    section; rows with surviving evidence are reported as ``retained`` instead.

    The three cases, all handled correctly:

    * formed by a deleted section, backed only by deleted sections -> **retired**
    * formed by a deleted section, also backed by a surviving one  -> **retained**
    * formed by a surviving section -> **not a candidate**, because that page
      still asserts it. ``metadata.custom_id`` records the section that FORMED the
      row (reinforcement does not rewrite it), which is exactly the right key for
      this filter.

    Retirement is ``forget_memory``: soft retire. The row leaves current retrieval
    but its evidence and ``as_of`` history survive (INGEST.md G3-5).
    """
    if not deleted_ids:
        return {"candidates": 0, "retired": 0, "retained": 0, "details": []}

    deleted = set(deleted_ids)
    graph = client.export_graph()
    retired: list[str] = []
    retained: list[dict[str, Any]] = []

    for relationship in graph.get("relationships", []):
        properties = relationship.get("properties", {})
        if relationship.get("type") == "MENTIONS" or properties.get("status") != "active":
            continue
        uuid = relationship.get("uuid")
        if not uuid:
            continue
        # Episode metadata is inherited onto the fact as properties["metadata"],
        # so the section coordinate lives one level down, not at the top.
        custom_id = (properties.get("metadata") or {}).get("custom_id")
        if custom_id not in deleted:
            continue
        evidence = await client.memory_evidence(relationship_uuid=uuid)
        backing = {
            episode.metadata.get("shadow_custom_id") or episode.metadata.get("custom_id")
            for episode in evidence.episodes
        }
        backing.discard(None)
        if backing and not backing.issubset(deleted):
            retained.append({"uuid": uuid, "fact": evidence.fact, "surviving": sorted(backing - deleted)})
            continue
        if apply:
            await client.forget_memory(
                relationship_uuid=uuid,
                reason=f"section_deleted:{custom_id}",
            )
        retired.append(uuid)

    return {
        "candidates": len(retired) + len(retained),
        "retired": len(retired),
        "retained": len(retained),
        "details": retained[:10],
    }


# --------------------------------------------------------------------------
# Dream + receipts
# --------------------------------------------------------------------------


async def run_dreams(
    client, *, scopes: list[MemoryScope], verbose: bool = True, max_formation_sweeps: int = 8
) -> dict[str, Any]:
    """Run formation -> consolidation -> pruning, in that order.

    ``tenant_id``/``scope`` are deliberately NOT passed: those kwargs route through
    ``_runtime_policy`` and require a ``MemoryControlPlane``. This pilot builds a
    ``Memotron`` with an explicit ``DreamConfig`` instead, so the jobs run
    unpinned and pick up every queued episode across both KB scopes -- which is
    what we want, since both scopes belong to this one tenant.
    """
    runs: list[Any] = []

    def _report(run: Any, elapsed: float) -> None:
        if verbose:
            print(
                f"    {run.job_name:<18} episodes={run.processed_episodes:<4} "
                f"scopes={run.processed_scopes:<3} "
                f"created={run.created_relationships:<4} "
                f"reinforced={run.reinforced_relationships:<3} "
                f"superseded={run.superseded_relationships:<3} "
                f"pruned={run.pruned_relationships:<3} "
                f"decisions={run.decision_count:<3} "
                f"({elapsed:.1f}s)"
            )

    # Formation is DRAINED, not run once. A single `run_dream_job` pass does not
    # necessarily consume every queued episode -- measured on this pilot, one pass
    # processed 19 of 37 and left 18 pending. Loop until a pass makes no progress
    # so `pending_episode_count` actually reaches zero.
    for sweep in range(1, max_formation_sweeps + 1):
        started = time.time()
        if verbose:
            print(f"  running {kb.FORMATION_JOB} (sweep {sweep}) ...", flush=True)
        result = await client.run_dream_job(job_name=kb.FORMATION_JOB)
        processed = 0
        for run in result.job_runs:
            runs.append((None, run, time.time() - started))
            processed += run.processed_episodes
            _report(run, time.time() - started)
        pending = 0
        for scope in scopes:
            evolution = await client.memory_evolution(scope=scope)
            pending += evolution.pending_episode_count
        if verbose:
            print(f"    -> {pending} episode(s) still pending")
        if pending == 0 or processed == 0:
            break

    for job in (kb.CONSOLIDATION_JOB, kb.PRUNING_JOB):
        started = time.time()
        if verbose:
            print(f"  running {job} ...", flush=True)
        result = await client.run_dream_job(job_name=job)
        for run in result.job_runs:
            runs.append((None, run, time.time() - started))
            _report(run, time.time() - started)
    return {"runs": runs}


async def collect_rejections(client, runs: list[Any]) -> Counter:
    """Count receipted candidate rejections by machine violation code."""
    codes: Counter = Counter()
    for _scope, run, _elapsed in runs:
        if not run.run_uuid:
            continue
        for receipt in await client.memory_receipts(run_uuid=run.run_uuid):
            if receipt.decision_type != ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED:
                continue
            try:
                payload = json.loads(receipt.event_payload or "{}")
            except json.JSONDecodeError:
                payload = {}
            codes[payload.get("violation", "unknown")] += 1
    return codes


async def collect_receipt_kinds(client, runs: list[Any]) -> Counter:
    kinds: Counter = Counter()
    for _scope, run, _elapsed in runs:
        if not run.run_uuid:
            continue
        for receipt in await client.memory_receipts(run_uuid=run.run_uuid):
            kinds[str(receipt.decision_type)] += 1
    return kinds


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_plan(plan: SyncPlan, cost: dict[str, float], *, limit: int | None) -> None:
    print("-" * 72)
    print("SYNC PLAN")
    print("-" * 72)
    print(f"  new              {len(plan.new)}")
    print(f"  changed          {len(plan.changed)}")
    print(f"  unchanged        {len(plan.unchanged)}   (skipped: no episode, no LLM call)")
    print(f"  deleted          {len(plan.deleted)}")
    if limit:
        print(f"  --limit          {limit}  (capping the ingest set)")
    print(f"\n  episodes         {int(cost['episodes'])}")
    print(f"  est input tok    {cost['input_tokens']:,.0f}")
    print(f"  est output tok   {cost['output_tokens']:,.0f}")
    print(f"  est extraction   ${cost['extraction_usd']:.2f}")
    print(f"  est embeddings   ${cost['embedding_usd']:.3f}")
    print(f"  EST TOTAL        ${cost['total_usd']:.2f}")
    print("-" * 72)


async def print_graph_summary(client, scopes: list[MemoryScope]) -> dict[str, Any]:
    graph = client.export_graph()
    nodes = graph.get("nodes", [])
    relationships = graph.get("relationships", [])
    active = [
        r for r in relationships if r.get("type") != "MENTIONS" and r.get("properties", {}).get("status") == "active"
    ]
    entity_nodes = [n for n in nodes if "Entity" in (n.get("labels") or ())]
    rollups = [r for r in active if r.get("type") == "ROLLUP"]

    print("-" * 72)
    print("GRAPH")
    print("-" * 72)
    print(f"  nodes                    {len(nodes)}  (Entity: {len(entity_nodes)})")
    print(f"  relationships (all)      {len(relationships)}")
    print(f"  active facts             {len(active)}")
    print(f"  ROLLUP facts             {len(rollups)}")
    print(f"  memory type distribution {graph.get('memory_type_distribution', {})}")

    for scope in scopes:
        evolution = await client.memory_evolution(scope=scope)
        if evolution.episode_count == 0:
            continue
        print(f"\n  [{scope.key}]")
        print(
            f"    episodes                {evolution.episode_count}"
            f" (processed {evolution.processed_episode_count},"
            f" pending {evolution.pending_episode_count})"
        )
        print(f"    active facts            {evolution.active_relationship_count}")
        print(f"    context-visible         {evolution.context_visible_relationship_count}")
        print(f"    rollups                 {evolution.rollup_relationship_count}")
        print(f"    demoted into rollups    {evolution.demoted_relationship_count}")
        print(
            f"    created / reinforced    {evolution.created_relationship_count}"
            f" / {evolution.reinforced_relationship_count}"
        )
        print(
            f"    superseded / pruned     {evolution.superseded_relationship_count}"
            f" / {evolution.pruned_relationship_count}"
        )
        print(f"    semantic dedup rate     {evolution.semantic_dedup_rate:.3f}")
        print(f"    compression ratio       {evolution.compression_ratio:.3f}")
        print(f"    tokens raw episodes     {evolution.tokens_raw_episodes:,}")
        print(f"    tokens rendered profile {evolution.tokens_rendered_profile:,}")
        print(f"    tokens saved vs raw     {evolution.tokens_saved_vs_raw:,}")
        print(f"    per-type active         {evolution.per_type_active_counts}")
    print("-" * 72)
    return {"nodes": len(nodes), "active": len(active), "rollups": len(rollups)}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def do_reset() -> None:
    graph_dir = kb.GRAPH_PATH.parent
    kb.assert_isolation()
    if graph_dir.exists():
        shutil.rmtree(graph_dir)
        print(f"  removed {graph_dir}")
    if kb.MANIFEST_PATH.exists():
        kb.MANIFEST_PATH.unlink()
        print(f"  removed {kb.MANIFEST_PATH}")
    print("  reset complete")


async def run(args: argparse.Namespace) -> int:
    kb.assert_isolation()
    print("=" * 72)
    print("JedAI portal KB sync")
    print("=" * 72)
    print(f"  tenant     {kb.TENANT_ID}")
    print(f"  graph      {kb.GRAPH_PATH}")
    print(f"  protected  {kb.PROTECTED_GRAPH} (never opened)")
    print("  ASSERT OK  pilot graph != spymaster graph")

    scope_names = ["external", "internal"] if args.scope == "both" else [args.scope]
    scopes = [kb.SCOPES[name] for name in scope_names]
    print(f"  scopes     {', '.join(s.key for s in scopes)}")

    print("\npreprocessing ...")
    sections = pre.preprocess(
        content_root=args.content_root,
        subtrees=tuple(args.subtrees) if args.subtrees else pre.PILOT_SUBTREES,
    )
    allowed = {s.key for s in scopes}
    sections = [s for s in sections if kb.scope_for(s.frontmatter).key in allowed]
    if args.only:
        sections = [s for s in sections if any(fragment in s.source_path for fragment in args.only)]
        print(f"  --only {args.only}")
    print(f"  {len(sections)} sections in scope")

    manifest = load_manifest(kb.MANIFEST_PATH)
    # A filtered run cannot see the whole corpus, so it must never conclude that
    # the sections it was told to ignore have been deleted.
    filtered = bool(args.only or args.subtrees or args.limit or args.scope != "both")
    plan = build_plan(sections, manifest, detect_deletions=not filtered)
    if filtered and manifest:
        print(
            "  deletion detection DISABLED for this filtered run "
            "(--only/--subtree/--limit/--scope); run unfiltered to retire removed sections"
        )
    to_ingest = plan.to_ingest
    if args.limit:
        to_ingest = to_ingest[: args.limit]
    cost = estimate_cost(to_ingest, input_rate=args.input_rate, output_rate=args.output_rate)
    print_plan(plan, cost, limit=args.limit)

    if args.dry_run:
        print("\n--dry-run: zero gateway calls made. Nothing was written.")
        if plan.deleted:
            print(f"\n  would evaluate {len(plan.deleted)} deleted section(s) for retirement:")
            for custom_id in plan.deleted[:10]:
                print(f"    {custom_id}")
        return 0

    if not to_ingest and not plan.deleted and not args.dream_only:
        print("\nnothing to do -- every section is unchanged.")
        return 0

    kb.require_gateway_key()
    client = kb.build_client()
    started = time.time()
    try:
        if args.dream_only:
            to_ingest = []
            print("\n--dream-only: skipping ingest, draining the existing episode queue")
        if to_ingest:
            print(f"\ningesting {len(to_ingest)} section(s) ...")
            ingested = await ingest_sections(client, to_ingest, verbose=not args.quiet)
            print(f"  {ingested['chunks']} episode chunk(s) queued")
        else:
            ingested = {"chunks": 0, "scope_keys": {}}

        print("\ndreaming ...")
        dreams = await run_dreams(client, scopes=scopes, verbose=not args.quiet)

        if plan.deleted:
            print(f"\nretiring {len(plan.deleted)} deleted section(s) ...")
            retirement = await retire_deleted(client, plan.deleted, scopes, apply=True)
            print(f"  retired  {retirement['retired']}")
            print(f"  retained {retirement['retained']} (evidence survives on other sections)")
            for detail in retirement["details"]:
                print(f"    keep {detail['uuid'][:8]} -- also stated by {detail['surviving']}")

        elapsed = time.time() - started
        print()
        await print_graph_summary(client, scopes)

        rejections = await collect_rejections(client, dreams["runs"])
        print("\nCANDIDATE REJECTIONS (receipted, by machine violation code)")
        if rejections:
            for code, count in rejections.most_common():
                print(f"  {code:<40} {count}")
            print(f"  {'TOTAL':<40} {sum(rejections.values())}")
        else:
            print("  none")

        kinds = await collect_receipt_kinds(client, dreams["runs"])
        print("\nRECEIPT KINDS")
        for kind, count in kinds.most_common(18):
            print(f"  {kind:<44} {count}")

        print(f"\nwall time {elapsed:.1f}s")
        print(f"est cost  ${cost['total_usd']:.2f} (gateway is the billing authority)")

        # Manifest records only what actually made it in.
        merged = dict(manifest)
        scope_keys = ingested.get("scope_keys", {})
        for section in to_ingest:
            merged[section.custom_id] = {
                "content_sha256": section.content_sha256,
                "slug": section.slug,
                "anchor": section.anchor,
                "reference_time": section.reference_time,
                "chars": section.chars,
                "scope_key": scope_keys.get(section.custom_id, ""),
            }
        for custom_id in plan.deleted:
            merged.pop(custom_id, None)
        kb.MANIFEST_PATH.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "written_at": datetime.now().astimezone().isoformat(),
                    "tenant_id": kb.TENANT_ID,
                    "sections": merged,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        print(f"manifest  {len(merged)} section(s) -> {kb.MANIFEST_PATH}")
    finally:
        client.graph.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portal -> Memotron incremental sync")
    parser.add_argument("--content-root", type=Path, default=pre.DEFAULT_CONTENT_ROOT)
    parser.add_argument("--subtree", action="append", dest="subtrees", default=None)
    parser.add_argument("--dry-run", action="store_true", help="plan + cost estimate, zero calls")
    parser.add_argument("--limit", type=int, default=None, help="cap sections ingested this run")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="PATH_FRAGMENT",
        help="restrict to source paths containing this fragment (repeatable)",
    )
    parser.add_argument("--scope", choices=("external", "internal", "both"), default="both")
    parser.add_argument("--reset", action="store_true", help="wipe the pilot graph and manifest")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--dream-only",
        action="store_true",
        help="skip ingest; drain the queued episodes and re-run consolidation/pruning",
    )
    parser.add_argument("--input-rate", type=float, default=DEFAULT_INPUT_RATE)
    parser.add_argument("--output-rate", type=float, default=DEFAULT_OUTPUT_RATE)
    args = parser.parse_args(argv)

    if args.reset:
        do_reset()
        if not any([args.dry_run, args.limit]):
            return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
