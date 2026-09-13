"""T1-20: are a checkpoint's graph-state hashes covered by the thing that makes it tamper-evident?

Filed claim (AGENT-SOURCED): `run_checkpoints.graph_state_hash_before/after` sit outside the
Merkle commitment and outside every verification path.

Source says yes: `merkle_root` is computed over receipt hashes only
(`storage/receipts.py:1134`), and chain verification compares exactly four fields --
`receipt_count`, `first_receipt_hash`, `last_receipt_hash`, `merkle_root`
(`storage/receipts.py:1351-1364`). Neither graph-state column appears.

But "not referenced in the code I read" is how D-45 happened, so this TAMPERS with the column
directly and asks the verifier. A verifier that still returns valid has told us the column is
unprotected, by its own behaviour rather than by my reading.

The control matters: the same probe also tampers with a field the checkpoint DOES commit to
(`last_receipt_hash`). If that is not caught either, the verifier is simply not working and the
first result means nothing.

    uv run python scripts/verify/probe_checkpoint_coverage.py

exit 0 = tampering with the graph-state hash is detected  ·  1 = it is not
exit 2 = the control tamper was not detected either, so no arm is interpretable
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")

from memotron import Memotron, MemoryScope, ScopeKind

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="ckpt")


async def build(path: Path) -> str:
    """Seed a graph and return a run_uuid that has a persisted checkpoint."""
    dw = Memotron(graph_path=str(path))
    for i in range(3):
        await dw.add_memory(
            subject=f"service {i}",
            predicate="requires",
            object=f"dependency {i}",
            relationship_type="REQUIRES",
            scope=SCOPE,
        )
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT run_uuid, graph_state_hash_after, last_receipt_hash FROM run_checkpoints LIMIT 1"
    ).fetchone()
    con.close()
    return row


def verify(path: Path, run_uuid: str) -> bool:
    dw = Memotron(graph_path=str(path))
    return dw.graph.receipts.verify_chain(run_uuid).valid


def tamper(path: Path, column: str, run_uuid: str) -> None:
    con = sqlite3.connect(path)
    con.execute(f"UPDATE run_checkpoints SET {column} = ? WHERE run_uuid = ?", ("deadbeef" * 8, run_uuid))
    con.commit()
    con.close()


async def main() -> int:
    results = {}
    for column in ("graph_state_hash_after", "last_receipt_hash"):
        tmp = Path(tempfile.mkdtemp(prefix="dwckpt-")) / "g.sqlite"
        row = await build(tmp)
        if row is None:
            print("  PRECONDITION FAILED: no run_checkpoints row was written at all.")
            return 2
        run_uuid = row[0]
        if not verify(tmp, run_uuid):
            print("  PRECONDITION FAILED: the chain was already invalid before tampering.")
            return 2
        tamper(tmp, column, run_uuid)
        still_valid = verify(tmp, run_uuid)
        results[column] = still_valid
        verdict = "NOT DETECTED" if still_valid else "detected"
        print(f"    tampered {column:24s} -> verify_chain says {'valid' if still_valid else 'INVALID'}   ({verdict})")

    print()
    if results["last_receipt_hash"]:
        print("  === INCONCLUSIVE ===")
        print("  The control tamper (a field the checkpoint provably commits to) was not caught")
        print("  either, so the verifier is not doing its job in this setup and neither arm means")
        print("  anything.")
        return 2
    if results["graph_state_hash_after"]:
        print("  === CONFIRMED: the graph-state hash is outside the tamper-evident boundary ===")
        print("  Rewriting what the graph looked like after a dream run leaves verification green,")
        print("  while rewriting a committed field is caught immediately. The checkpoint attests to")
        print("  the receipt chain, not to the graph state it records.")
        return 1
    print("  === REFUTED: tampering with the graph-state hash was detected ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
