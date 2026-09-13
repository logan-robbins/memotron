"""Scale benchmark: does cost grow with TOTAL graph size, and does relevance survive?

Closes both halves of U-1 and tests T1-7's derived O(total rows) claim.

Method: seed N facts across several subjects plus one known NEEDLE fact, then time
retrieval and the per-episode context builder, and assert the needle still surfaces.
No LLM calls — uses client-managed writes so the numbers are deterministic.

    uv run python scripts/verify/bench_scale.py [sizes...]      # default 10 100 500
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import statistics
import sys
import time
from datetime import UTC, datetime

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.models import Episode, EpisodeType

NEEDLE_SUBJ = "the pinnacle deployment"
NEEDLE_OBJ = "a rotated KMS customer master key before the Frankfurt cutover"
SUBJECTS = [
    "the api service",
    "the worker",
    "the ingest job",
    "the admin console",
    "the gateway",
    "the scheduler",
    "the export job",
    "the webhook relay",
]
OBJECTS = [
    "a durable store",
    "a retry budget",
    "an idle timeout",
    "a circuit breaker",
    "a dead letter queue",
    "a rate limit",
    "a bulkhead",
    "a health probe",
]


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t0) * 1000


async def atimed(coro):
    t0 = time.perf_counter()
    out = await coro
    return out, (time.perf_counter() - t0) * 1000


async def run_size(n: int) -> dict:
    path = f"/tmp/bench/g{n}.sqlite"
    os.makedirs("/tmp/bench", exist_ok=True)
    for suffix in ("", ".kek", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path + suffix)

    dw = Memotron(graph_path=path)
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="bench")

    t0 = time.perf_counter()
    for i in range(n):
        await dw.add_memory(
            subject=f"{SUBJECTS[i % len(SUBJECTS)]} {i // len(SUBJECTS)}",
            predicate="requires",
            object=f"{OBJECTS[i % len(OBJECTS)]} variant {i}",
            relationship_type="REQUIRES",
            scope=scope,
        )
    # the needle — one distinctive fact we will look for
    await dw.add_memory(
        subject=NEEDLE_SUBJ, predicate="requires", object=NEEDLE_OBJ, relationship_type="REQUIRES", scope=scope
    )
    seed_ms = (time.perf_counter() - t0) * 1000

    # --- retrieval timings (median of 5) ---
    def med(samples):
        return statistics.median(samples)

    search_ms, sem_ms, prof_ms = [], [], []
    for _ in range(5):
        _, ms = await atimed(dw.search(query="rotated KMS customer master key", scope=scope))
        search_ms.append(ms)
        _, ms = await atimed(dw.semantic_search(query="rotated KMS customer master key", scope=scope))
        sem_ms.append(ms)
        _, ms = await atimed(dw.profile(scope=scope))
        prof_ms.append(ms)

    # --- the per-episode context builder: the T1-7 hot path ---
    eng = dw._engine
    ep = Episode(name="bench", body="bench", source=EpisodeType.TEXT, scope=scope, reference_time=datetime.now(UTC))
    from memotron.config import DreamContextPolicy

    ctx_ms = []
    for _ in range(5):
        _, ms = timed(lambda: eng._graph_context_for_episode(episode=ep, policy=DreamContextPolicy()))
        ctx_ms.append(ms)

    # --- relevance: does the needle still surface? ---
    hits = await dw.search(query="rotated KMS customer master key", scope=scope)
    sem = await dw.semantic_search(query="rotated KMS customer master key", scope=scope)
    found_kw = any(NEEDLE_OBJ[:24] in h.fact for h in hits)
    found_sem = any(NEEDLE_OBJ[:24] in h.fact for h in sem)
    rank_kw = next((i + 1 for i, h in enumerate(hits) if NEEDLE_OBJ[:24] in h.fact), None)

    total_rows = len(dw.graph.relationships())
    return {
        "n": n,
        "rows": total_rows,
        "seed_ms": seed_ms,
        "search": med(search_ms),
        "semantic": med(sem_ms),
        "profile": med(prof_ms),
        "ctx": med(ctx_ms),
        "found_kw": found_kw,
        "found_sem": found_sem,
        "rank_kw": rank_kw,
        "hits": len(hits),
        "sem_hits": len(sem),
    }


async def main() -> int:
    sizes = [int(a) for a in sys.argv[1:]] or [10, 100, 500]
    rows = []
    for n in sizes:
        print(f"  seeding {n}…", flush=True)
        rows.append(await run_size(n))

    print(
        f"\n  {'facts':>6} {'rows':>6} {'seed ms':>9} {'search':>8} {'semantic':>9} "
        f"{'profile':>8} {'ctx/ep':>8}   needle"
    )
    for r in rows:
        needle = ("kw#{}".format(r["rank_kw"])) if r["found_kw"] else "KW MISS"
        needle += " · sem ok" if r["found_sem"] else " · SEM MISS"
        print(
            f"  {r['n']:>6} {r['rows']:>6} {r['seed_ms']:>9.0f} {r['search']:>8.1f} "
            f"{r['semantic']:>9.1f} {r['profile']:>8.1f} {r['ctx']:>8.1f}   {needle}"
        )

    base, last = rows[0], rows[-1]
    grow = last["rows"] / max(base["rows"], 1)
    print(
        f"\n  graph grew {grow:.0f}×  →  "
        f"search {last['search'] / max(base['search'], 0.01):.1f}×  "
        f"semantic {last['semantic'] / max(base['semantic'], 0.01):.1f}×  "
        f"profile {last['profile'] / max(base['profile'], 0.01):.1f}×  "
        f"ctx/episode {last['ctx'] / max(base['ctx'], 0.01):.1f}×"
    )
    misses = [r for r in rows if not r["found_kw"] or not r["found_sem"]]
    print(
        "  relevance: "
        + ("needle found at every size" if not misses else f"NEEDLE LOST at sizes {[m['n'] for m in misses]}")
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
