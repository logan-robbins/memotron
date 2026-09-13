"""Does the audit chain really survive a crypto-shred?

This is the system's strongest claim and the headline of the takeover walkthrough:

    "you can destroy the key, make the content permanently unreadable, and every hash in
     the chain still verifies"

`crypto_shred`'s own docstring commits to it explicitly -- *"the hash chain still verifies
and every recorded run still byte-replays"*. Until this probe, that claim rested entirely on
**reading** the code and the design docs. Presenting it unverified would be exactly the
mistake this project has already made twice (D-45, D-50).

Four things must all hold, and each is asserted separately so a partial failure is legible:

  1. BEFORE  content is readable, the chain verifies, the run byte-replays
  2. SHRED   destroying the DEK reports success
  3. AFTER   the content is genuinely unreadable -- not merely "flagged"
  4. AFTER   the chain STILL verifies and the run STILL byte-replays

Step 3 is the one that could quietly be a no-op: a system can mark rows erased while the
plaintext sits on disk, and every audit assertion would still pass. So the probe reads the
raw stored bytes and requires the plaintext to be **absent** from them.

    uv run python scripts/verify/probe_shred_audit.py

exit 0 = erasure and audit both hold · 1 = a claim failed · 2 = harness error
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.config import ErasureBehavior, GovernancePolicy, default_config

# A distinctive plaintext: if this string survives anywhere in the stored bytes after the
# shred, the erasure did not happen regardless of what any API reports.
SECRET = "quarterly-revenue-was-4171-million-in-the-frankfurt-region"  # noqa: S105

FACTS = (
    ("the frankfurt region", "reported", SECRET, "REQUIRES"),
    ("the operator", "prefers", "postgres for the operational store", "PREFERS"),
    ("the worker", "requires", "a claim lock before processing", "REQUIRES"),
)


def raw_bytes_contain(graph_path: Path, needle: str) -> bool:
    """Is the plaintext present anywhere in the stored file, including the WAL?

    Read outside the library on purpose: the point is to observe the bytes on disk rather
    than ask the code under test whether it erased them.
    """
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(graph_path) + suffix)
        if p.exists() and needle.encode() in p.read_bytes():
            return True
    return False


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dwshred-"))
    graph_path = tmp / "graph.sqlite"

    # Crypto-shred governance, applied config-wide so every write in this probe is sealed.
    config = default_config().model_copy(
        update={"governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)}
    )
    dw = Memotron(graph_path=str(graph_path), config=config)
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="shred-audit")

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # ---------------------------------------------------------------- write
    print("\n  === write, sealed under a per-scope DEK ===")
    for subject, predicate, obj, rel_type in FACTS:
        await dw.add_memory(
            subject=subject,
            predicate=predicate,
            object=obj,
            relationship_type=rel_type,
            scope=scope,
        )
    rows = list(dw.graph.relationships_for_scope(scope.key))
    check("3 memories written", len(rows) == len(FACTS), f"{len(rows)} rows")

    # Is the content actually sealed on disk, or stored as plaintext all along?
    plaintext_on_disk_before = raw_bytes_contain(graph_path, SECRET)
    print(f"    (plaintext present in stored bytes BEFORE shred: {plaintext_on_disk_before})")

    # ---------------------------------------------------------------- before
    print("\n  === BEFORE the shred ===")
    hits = await dw.search(query="frankfurt region", scope=scope)
    readable = any(SECRET in (h.fact or "") for h in hits)
    check("content is readable through the API", readable, f"{len(hits)} hit(s)")

    runs = await dw.run_checkpoints(scope=scope)
    check("a receipted run exists", bool(runs), f"{len(runs)} run(s)")
    if not runs:
        print("\n  cannot verify a chain with no runs; stopping.")
        return 2

    # Verify EVERY run, not just one. A single-receipt chain has no
    # previous_receipt_hash link to check, so verifying one short run would be a
    # near-vacuous pass. Report the total so the strength of the check is visible.
    before = {r.run_uuid: dw.graph.receipts.verify_chain(run_uuid=r.run_uuid) for r in runs}
    total_before = sum(v.receipt_count for v in before.values())
    linked = sum(1 for v in before.values() if v.receipt_count > 1)
    check(
        "every run's hash chain verifies",
        all(v.valid for v in before.values()),
        f"{len(before)} run(s), {total_before} receipts total, {linked} with >1 receipt (i.e. an actual chain link)",
    )

    # Replay EVERY run before the shred too. Without this the "after" failures cannot be
    # attributed: a run that never replayed was not broken by the shred.
    replay_before_fail = []
    for r in runs:
        try:
            await dw.byte_replay(run_uuid=r.run_uuid)
        except Exception as exc:
            replay_before_fail.append(f"{r.run_uuid[:8]}: {type(exc).__name__}")
    check("every run byte-replays", not replay_before_fail, f"{len(runs)} run(s), failures={replay_before_fail[:3]}")

    # ---------------------------------------------------------------- shred
    print("\n  === crypto-shred the scope ===")
    summary = await dw.crypto_shred(scope=scope)
    check("shred reports success", bool(summary.get("shredded")), str(summary)[:120])

    # ---------------------------------------------------------------- after
    print("\n  === AFTER the shred: is the content really gone? ===")
    plaintext_after = raw_bytes_contain(graph_path, SECRET)
    if plaintext_on_disk_before:
        # Only a meaningful test when the plaintext WAS on disk beforehand.
        check(
            "plaintext removed from the stored bytes",
            not plaintext_after,
            "still on disk" if plaintext_after else "gone from file and WAL",
        )
    else:
        # Content was sealed from the first write, so "absent after" proves nothing about
        # the shred. Say so rather than banking a vacuous PASS -- the load-bearing check
        # is that the ciphertext is no longer decryptable, asserted through the API below.
        print(
            "    n/a   plaintext was never on disk (sealed from the first write), so this "
            "check cannot distinguish erasure from never-stored"
        )
        check("no plaintext on disk", not plaintext_after, "still true after the shred")

    try:
        hits_after = await dw.search(query="frankfurt region", scope=scope)
        leaked = [h for h in hits_after if SECRET in (h.fact or "")]
        check(
            "content unreadable through the API",
            not leaked,
            f"{len(hits_after)} hit(s), {len(leaked)} leaking plaintext",
        )
    except Exception as exc:
        # An exception is an acceptable way to be unreadable, as long as it is not a crash
        # in unrelated code.
        check("content unreadable through the API", True, f"raised {type(exc).__name__} (acceptable)")

    print("\n  === AFTER the shred: does the audit plane survive? ===")
    after = {r.run_uuid: dw.graph.receipts.verify_chain(run_uuid=r.run_uuid) for r in runs}
    total_after = sum(v.receipt_count for v in after.values())
    check(
        "every run's hash chain STILL verifies",
        all(v.valid for v in after.values()),
        f"{len(after)} run(s), {total_after} receipts total",
    )
    check("no receipt lost to the shred", total_after == total_before, f"{total_before} -> {total_after}")
    roots_before = {k: v.computed_merkle_root for k, v in before.items()}
    roots_after = {k: v.computed_merkle_root for k, v in after.items()}
    check(
        "every merkle root unchanged",
        roots_before == roots_after,
        f"{sum(1 for k in roots_before if roots_before[k] != roots_after.get(k))} changed",
    )

    replay_fail = []
    for r in runs:
        try:
            await dw.byte_replay(run_uuid=r.run_uuid)
        except Exception as exc:
            replay_fail.append(f"{r.run_uuid[:8]}: {type(exc).__name__}")
    newly_broken = [f for f in replay_fail if f not in replay_before_fail]
    check(
        "the shred broke no run that previously replayed",
        not newly_broken,
        f"before={len(replay_before_fail)} after={len(replay_fail)} newly broken={newly_broken[:3]}",
    )

    print("\n  === the machine-verifiable erasure proof ===")
    try:
        cert = await dw.erasure_certificate(scope=scope)
        verified = await dw.verify_erasure(scope=scope, certificate=cert)
        check("erasure certificate verifies", bool(verified), type(cert).__name__)
    except Exception as exc:
        check("erasure certificate verifies", False, f"{type(exc).__name__}: {exc}")

    print(f"\n=== shred/audit: {'FAIL' if failures else 'PASS'} ===")
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("  content destroyed, audit plane intact, run still replayable.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
