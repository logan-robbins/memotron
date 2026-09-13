"""T0-1: does `migrate` move memory, or silently strand it?

T0-1 is the only Tier-0 item with no reproduction. Its mechanism is source-provable --
`migration.py` mentions `epoch_id` **zero times** and copies properties wholesale, while
`purge_source` defaults True at all three levels (`migration.py:771`, `cli.py:441`, and
hardcoded at `cli.py:372` inside `init`). But "silently destroys memory" is a *behavioural*
claim, and it has never been run.

**Why it could still be fine.** `migrate_tenant_memory`'s docstring describes a careful
COPY -> VERIFY -> LOCK -> RE-VERIFY -> PURGE ordering and states the outcome is *"duplicate
data or a loud failure, never silent"* loss. That deserves to be taken seriously.

**The gap the claim alleges, precisely.** Verification counts **raw rows**; the public read
path filters by **epoch**. A row can therefore be copied, counted, verified, and purged from
the source while being invisible in the destination -- passing every check the docstring
describes. This probe measures both numbers separately, because only their disagreement
distinguishes the two stories:

    raw rows in destination      -- what VERIFY counts
    rows visible via the API     -- what a user can actually read

Equal  -> migration is sound, T0-1's consequence is wrong however good its mechanism looks.
Diverge -> memory survived the copy, passed verification, and is unreachable. With
           purge_source defaulting True, the source is gone too.

    uv run python scripts/verify/probe_migration_visibility.py

exit 0 = everything migrated is readable · 1 = rows stranded · 2 = harness error
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from memotron import Memotron, MemoryScope, ScopeKind

SOURCE_TENANT = "alpha"
DEST_TENANT = "beta"

FACTS = (
    ("the api service", "requires", "a durable store", "REQUIRES"),
    ("the operator", "prefers", "postgres for the operational store", "PREFERS"),
    ("the worker", "requires", "a claim lock before processing", "REQUIRES"),
)


def visible_and_raw(dw: Memotron, scope: MemoryScope) -> tuple[int, int]:
    """(rows the public scoped read returns, raw non-MENTIONS rows in the store).

    The scoped read is what a user gets; the raw count is what migration verifies.
    """
    visible = [r for r in dw.graph.relationships_for_scope(scope.key) if r.type != "MENTIONS"]
    raw = [r for r in dw.graph.relationships() if r.type != "MENTIONS"]
    return len(visible), len(raw)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dwmig-"))
    src_path, dst_path = tmp / "source.sqlite", tmp / "dest.sqlite"

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # ------------------------------------------------------------------ seed
    print("\n  === seed the source tenant ===")
    src = Memotron(graph_path=str(src_path))
    src_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=SOURCE_TENANT)
    for s, p, o, t in FACTS:
        await src.add_memory(subject=s, predicate=p, object=o, relationship_type=t, scope=src_scope)
    vis, raw = visible_and_raw(src, src_scope)
    check("source holds the memories", vis == len(FACTS), f"visible={vis} raw={raw}")

    hits = await src.search(query="durable store", scope=src_scope)
    check("source memories are searchable", bool(hits), f"{len(hits)} hit(s)")

    # ------------------------------------------------------------------ migrate
    print("\n  === migrate alpha -> beta into a SEPARATE destination store ===")
    print("     (the realistic case: adopting into a new project, which mints its own epoch)")
    from memotron.migration import migrate_tenant_memory
    from memotron.storage import open_storage

    # CRITICAL PRECONDITION. T0-1 alleges rows go invisible "once the destination mints its
    # own root epoch". An EMPTY destination scope has no epoch at all, and _epoch_visible
    # treats a missing epoch as visible -- so migrating into a virgin store cannot trigger
    # the bug and a PASS there proves nothing. The destination scope must therefore already
    # hold its own memory, which is what mints its epoch, and is also the realistic case:
    # adopting one project's memory into a tenant that is already in use.
    dst_boot = Memotron(graph_path=str(dst_path))
    dst_boot_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=DEST_TENANT)
    await dst_boot.add_memory(
        subject="the destination tenant",
        predicate="already has",
        object="its own prior memory",
        relationship_type="REQUIRES",
        scope=dst_boot_scope,
    )
    pre_vis, pre_raw = visible_and_raw(dst_boot, dst_boot_scope)
    print(f"     destination pre-seeded so it owns an epoch: visible={pre_vis} raw={pre_raw}")
    PRE_EXISTING = pre_raw

    src_store = open_storage(str(src_path))
    dst_store = open_storage(str(dst_path))
    try:
        result = migrate_tenant_memory(
            src_store,
            source_tenant_id=SOURCE_TENANT,
            dest_tenant_id=DEST_TENANT,
            dest_store=dst_store,
            # left at its default on purpose -- that default IS the finding
        )
        migrated = getattr(result, "relationships_migrated", None)
        print(f"     migrate_tenant_memory reported: relationships_migrated={migrated}")
        check("migration reported success", migrated is None or migrated > 0, str(result)[:110])
    except Exception as exc:
        # A loud failure is the documented good outcome -- record it as such.
        print(f"     raised {type(exc).__name__}: {str(exc)[:150]}")
        check(
            "migration completed (a raise here is the SAFE documented outcome)", False, "loud failure, not silent loss"
        )
        src_store.close()
        dst_store.close()
        print(f"\n=== migration visibility: {'FAIL' if failures else 'PASS'} ===")
        print("  migration refused rather than stranding data -- T0-1's consequence not reproduced")
        return 1 if failures else 0
    finally:
        try:
            src_store.close()
            dst_store.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ read back
    print("\n  === destination: what VERIFY counted vs what a user can read ===")
    dst = Memotron(graph_path=str(dst_path))
    dst_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=DEST_TENANT)
    d_vis, d_raw = visible_and_raw(dst, dst_scope)
    expected = PRE_EXISTING + len(FACTS)
    print(f"     raw rows in destination   : {d_raw}   <- what migration verifies")
    print(f"     visible via scoped read   : {d_vis}   <- what a user gets")
    print(f"     expected                  : {expected}   ({PRE_EXISTING} pre-existing + {len(FACTS)} migrated)")
    check(
        "every migrated row is visible",
        d_vis == d_raw and d_raw == expected,
        f"raw={d_raw} visible={d_vis} expected={expected}",
    )

    try:
        d_hits = await dst.search(query="durable store", scope=dst_scope)
        check("migrated memories are searchable", bool(d_hits), f"{len(d_hits)} hit(s)")
    except Exception as exc:
        check("migrated memories are searchable", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ source
    print("\n  === source: was it purged? ===")
    src2 = Memotron(graph_path=str(src_path))
    s_vis, s_raw = visible_and_raw(src2, src_scope)
    print(f"     source raw={s_raw} visible={s_vis}")
    if s_raw == 0 and d_raw > 0 and d_vis == 0:
        check("data survives somewhere readable", False, "source purged AND destination unreadable -- this is the loss")

    print(f"\n=== migration visibility: {'FAIL' if failures else 'PASS'} ===")
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("  everything migrated is readable in the destination.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
