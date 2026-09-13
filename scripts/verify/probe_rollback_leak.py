"""T0-10's open half: does a rolled-back epoch's memory resurface through an entity hop?

T0-10 is **observed** for rows made out-of-ancestry by migration: retrieval's stage 1 uses
`relationships_for_scope` (epoch-filtered, `storage/sqlite/_graph.py:516` `relationships_for_scope`) while its entity-hop expansion uses
`relationships_for_node_uuids` (`storage/sqlite/_graph.py:564` `relationships_for_node_uuids`), which has **no epoch predicate and no
`_epoch_visible` call**.

The consequence recorded as **DERIVED** was that the same hole makes `rollback_epoch` unsound.
The derivation is strong — the leaking read cannot distinguish *why* a row is out-of-ancestry,
because it never looks — but "strong derivation" is what D-45, D-50 and D-58 all were before
they were wrong. So this runs it.

    branch -> adopt -> confirm the new memory is visible -> rollback -> ask again

**The question is precise:** after `redream_rollback`, does a fact that existed ONLY in the
rolled-back epoch still come back from `search()`? If it does, rollback hides a memory from the
epoch-filtered reads while retrieval keeps serving it — and "roll back the re-dream" is not a
guarantee.

Needs a live extractor: `redream_branch` recomputes over episodes through the LLM path, so run
it with gateway credentials in the environment.

    set -a; . ./.env; set +a
    uv run python scripts/verify/probe_rollback_leak.py

exit 0 = rollback fully hides the rolled-back memory · 1 = it leaks · 2 = harness error

CITATIONS REPAIRED 2026-08-31: every `file.py:NNN` below was rewritten after the module
split dissolved the god files they named. Each now carries the SYMBOL as well as the line,
so the next move makes them findable by grep rather than silently wrong.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.epochs import EpisodeSelector
from memotron.models import EpisodeType

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="rollback-leak")

# A fact that will exist ONLY inside the branched epoch. Distinctive so it cannot be confused
# with anything the baseline produced.
MARKER = "the frankfurt cutover requires a rotated customer master key"

# The episode must be SUBSTANTIVE, not a single sentence. D-46 measured this: gateway
# extraction returns zero candidates for a thin fragment and scales with content. A one-line
# body produced `created_relationships=0, admitted_candidates=0` here -- the precondition
# silently failed and every later assertion passed vacuously.
EPISODE_BODY = (
    "Session summary. " + MARKER + ". "
    "Ryan confirmed the rotation must happen before the Frankfurt cutover window opens. "
    "The operator should verify the key rotation completed before enabling the new region. "
    "Blocker: the Frankfurt cutover cannot proceed until that customer master key is rotated."
)


async def visible(dw: Memotron) -> tuple[int, int, list[str]]:
    """(scoped-read count, search hits for the marker, facts search returned)."""
    scoped = [r for r in dw.graph.relationships_for_scope(SCOPE.key) if r.type != "MENTIONS"]
    try:
        hits = await dw.search(query="frankfurt cutover rotated customer master key", scope=SCOPE)
    except Exception:
        hits = []
    return len(scoped), len(hits), [str(h.fact)[:60] for h in hits]


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dwrb-"))
    # The extractor must be wired EXPLICITLY. Memotron(graph_path=...) does not consult the
    # environment (T1-16): it takes the default InstructionalExtractor, which produced zero
    # candidates from a four-sentence factual episode even with LITELLM_API_KEY set. Passing
    # the env-built transports is what makes formation actually happen here.
    from memotron.runtime import build_transports_from_env

    extraction_transport, dream_agent_transport = build_transports_from_env()
    print(f"  extractor: {type(extraction_transport).__name__}")

    # strict_properties must be relaxed or nothing forms at all. T0-11: default_config()
    # inherits strict_properties=True and allows only (kind, role, tier, region, system), so a
    # real model's candidates are quarantined wholesale on `property_key_not_allowed` -- 3 of 3,
    # silently. agent_memory_config() sets it False, which is why the hook path works. We relax
    # it here so the probe can reach its actual subject; the strictness itself is T0-11's finding.
    from memotron.config import default_config

    cfg = default_config()
    iset = cfg.instruction_sets[0]
    relaxed_nodes = tuple(n.model_copy(update={"strict_properties": False}) for n in iset.node_instructions)
    cfg = cfg.model_copy(update={"instruction_sets": (iset.model_copy(update={"node_instructions": relaxed_nodes}),)})
    dw = Memotron(
        graph_path=str(tmp / "graph.sqlite"),
        config=cfg,
        extraction_transport=extraction_transport,
        dream_agent_transport=dream_agent_transport,
    )

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # ---------------------------------------------------------------- baseline
    print("\n  === baseline HEAD ===")
    await dw.add_memory(
        subject="the api service",
        predicate="requires",
        object="a durable store",
        relationship_type="REQUIRES",
        scope=SCOPE,
    )
    base_scoped, base_hits, _ = await visible(dw)
    print(f"     scoped={base_scoped}  marker hits={base_hits}")
    check("marker absent before the branch", base_hits == 0, f"{base_hits} hit(s)")

    # An episode carrying ONLY the marker fact, for the branch to recompute over.
    res = await dw.add_episode(
        name="frankfurt-cutover",
        episode_body=EPISODE_BODY,
        source=EpisodeType.TEXT,
        scope=SCOPE,
        reference_time=datetime.now(UTC),
    )
    ep_uuid = getattr(res, "episode_uuid", None)
    print(f"     queued episode {str(ep_uuid)[:8]} carrying the marker")

    # ---------------------------------------------------------------- branch + adopt
    print("\n  === branch, then adopt ===")
    try:
        # Tier A (the default) RE-GOVERNS stored candidates and does not re-extract, so a
        # freshly queued episode yields created_relationships=0 -- five earlier runs failed on
        # exactly that. epochs.py:170: extraction_transport/instruction_set force Tier C, a
        # full re-extract. Combined with the relaxed strict_properties above (T0-11), the
        # branch can finally produce rows that exist ONLY in the new epoch, which is the
        # precondition the rollback question needs.
        from memotron.epochs import RedreamOverrides

        branch = await dw.redream_branch(
            scope=SCOPE,
            selector=EpisodeSelector(episode_uuids=(ep_uuid,)),
            overrides=RedreamOverrides(extraction_transport=extraction_transport),
        )
        epoch_id = getattr(branch, "epoch_id", None) or getattr(branch, "new_epoch_id", None)
        # RedreamResult already answers "did the branch form anything?" -- it carries
        # created_relationships, episodes_selected and shadow_store_path. Reading only
        # epoch_id off it (as this probe first did) throws away exactly the diagnostic
        # that separates "the branch never formed" from "adopt dropped it".
        print(f"     branched epoch {str(epoch_id)[:24]}")
        for k in (
            "tier",
            "tier_reason",
            "episodes_selected",
            "baseline_relationships_seeded",
            "created_relationships",
            "reinforced_relationships",
            "superseded_relationships",
        ):
            print(f"       {k:32s} {getattr(branch, k, '?')}")
        print(f"       shadow_store_path                {str(getattr(branch, 'shadow_store_path', '?'))[-46:]}")
        await dw.redream_adopt(scope=SCOPE, epoch_id=epoch_id)
        print("     adopted")
    except Exception as exc:
        print(f"     branch/adopt raised {type(exc).__name__}: {str(exc)[:170]}")
        print("\n  cannot test rollback without a successful adopt -- harness error, not a finding")
        return 2

    ad_scoped, ad_hits, ad_facts = await visible(dw)
    print(f"     scoped={ad_scoped}  marker hits={ad_hits}  {ad_facts[:2]}")

    # PRECONDITION, NOT A RESULT. If the branch formed no memory there is nothing for the
    # rollback to hide, and every later assertion passes vacuously. Returning 1 here would
    # report "rollback leaks" on the strength of a test that never ran -- the exact mistake
    # D-62 caught. Inconclusive is exit 2.
    if not (ad_hits > 0 or ad_scoped > base_scoped):
        print(
            f"\n  PRECONDITION FAILED: the branch formed no new memory "
            f"(scoped {base_scoped}->{ad_scoped}, marker hits {ad_hits})."
        )
        print("  There is nothing for a rollback to hide, so the leak question CANNOT be")
        print("  answered by this run. Not a finding either way.")
        print("  Likely causes to eliminate first: the formation gate refusing the episode")
        print("  (T1-1), or extraction returning nothing for a one-line body (D-46).")
        print("\n=== rollback leak: INCONCLUSIVE ===")
        return 2

    # ---------------------------------------------------------------- rollback
    print("\n  === roll back ===")
    await dw.redream_rollback(scope=SCOPE)
    rb_scoped, rb_hits, rb_facts = await visible(dw)
    print(f"     scoped-read count : {rb_scoped}   (epoch-filtered path)")
    print(f"     search hits       : {rb_hits}   {rb_facts[:2]}   (expansion path)")

    # The finding: the epoch-filtered read hides it, retrieval does not.
    check(
        "rollback hides the rolled-back memory from the scoped read",
        rb_scoped <= base_scoped,
        f"scoped={rb_scoped} baseline={base_scoped}",
    )
    check(
        "rollback ALSO hides it from search()",
        rb_hits == 0,
        f"{rb_hits} hit(s) still returned after rollback: {rb_facts[:1]}",
    )

    print(f"\n=== rollback leak: {'LEAKS' if failures else 'no leak'} ===")
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("  rollback hid the memory from every read path tested.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
