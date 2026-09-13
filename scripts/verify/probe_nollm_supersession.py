"""Can anything the no-LLM path writes ever be superseded?

D-47 established that the rule-based path does not extract — it stores the whole sanitized
episode verbatim as one `HAS_STATE` fact. From that I DERIVED that such a blob has no
canonical predicate for the truth slot to key on, so two compactions would produce two
coexisting rows that never supersede. **That derivation may well be wrong**: both rows share
subject `claude-code` and predicate "has state", so the slot may key them together after all.
This tests it instead of assuming.

ANSWERED 2026-08-26 (D-48): **the derivation was wrong — supersession works.** The truth key
is `agent:claude-code:claude-code:has state`, i.e. subject + canonical predicate, which keys
both writes to the same slot. After the dream queue is drained the incumbent goes
`superseded` (observed_count 2) and the contradicting summary becomes the `active` row, with
the old row retained.

**The real finding is the timing.** Nothing forms at hook time. Until `run_due_dreams()` runs,
the graph keeps asserting the STALE fact — this probe watched it serve "Postgres" after the
user had said "SQLite". That is B5 (no dream worker exists) with teeth: without the worker,
memory does not merely go stale, it actively serves superseded facts.

This probe must therefore always judge POST-dream state; judging the pre-dream snapshot is
what made its first verdict wrong.

**Held constant:** one project, one graph, one scope, one transport (rule-based, credentials
stripped). The ONLY thing that varies between the two writes is the episode text, and the
second contradicts the first.

    uv run python scripts/verify/probe_nollm_supersession.py

exit 0 = rows supersede or dedup · 1 = rows accumulate unsuperseded · 2 = harness error
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIRST = "We decided the Memotron operational store will be Postgres, not SQLite."
SECOND = "We reversed that decision: the Memotron operational store will stay SQLite, not Postgres."

CHILD = r"""
import io, json, os, sys
from pathlib import Path
from memotron.adoption import (
    initialize_claude_code_project, load_project_config, save_project_config,
    build_platform_from_project, AdoptionStorage,
)
from memotron.cli import main as cli_main
from memotron.agent_memory import agent_scope

root = Path(sys.argv[1]); graph = Path(sys.argv[2])
first, second = sys.argv[3], sys.argv[4]

# rule-based only: strip every credential that would route to the gateway (T1-14 means the
# cwd .env can re-inject these, so the parent also runs us from a neutral cwd)
for k in ("LITELLM_API_KEY", "LITELLM_API_BASE", "MEMOTRON_LLM_MODEL",
          "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)

initialize_claude_code_project(project_root=root)
config = load_project_config(root).model_copy(
    update={"storage": AdoptionStorage(graph_path=str(graph))})
save_project_config(root, config)

def hook(session, summary):
    sys.argv = ["memotron", "hook", "post-compact", "--project-root", str(root)]
    sys.stdin = io.StringIO(json.dumps(
        {"session_id": session, "hook_event_name": "PostCompact", "compact_summary": summary}))
    try:
        cli_main()
    except SystemExit:
        pass

hook("s1", first)
import time
gap = float(os.environ.get("HOOK_GAP_SECONDS", "0"))
if gap:
    time.sleep(gap)          # the only variable vs D-48's back-to-back run
hook("s2", second)

platform, _ = build_platform_from_project(root)
g = platform.client.graph

def snapshot():
    return [{"status": (r.properties or {}).get("status"),
             "oc": (r.properties or {}).get("observed_count"),
             "fact": str((r.properties or {}).get("fact"))[:60]}
            for r in g.relationships() if r.type == "HAS_STATE"]

before = snapshot()

# Competing explanation: formation is DEFERRED, not lost. Drain the queue explicitly.
import asyncio
dream_err = None
try:
    asyncio.run(platform.client.run_due_dreams())
except Exception as e:
    dream_err = f"{type(e).__name__}: {e}"
after = snapshot()

rows = [r for r in g.relationships() if r.type == "HAS_STATE"]
out = []
for r in rows:
    p = r.properties or {}
    out.append({
        "status": p.get("status"),
        "truth_key": (str(p.get("truth_key"))[:46] if p.get("truth_key") else None),
        "valid_to": str(p.get("valid_to"))[:19] if p.get("valid_to") else None,
        "observed_count": p.get("observed_count"),
        "fact_head": str(p.get("fact"))[:70],
        "fact_len": len(str(p.get("fact") or "")),
    })
eps = g.episodes_for_scope("agent:claude-code")
ep_bodies = [str(getattr(e, "body", "")) for e in eps]
allrows = [{"type": r.type, "status": (r.properties or {}).get("status"),
            "fact": str((r.properties or {}).get("fact"))[:60]} for r in g.relationships()]
print("__R__" + json.dumps({"before_dreams": before, "after_dreams": after,
                            "dream_err": dream_err,
                            "has_state_rows": out, "total_rows": len(g.relationships()),
                            "episodes": len(eps), "episode_bodies": ep_bodies,
                            "all_rows": allrows}))
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dwsupers-"))
    root = tmp / "sample-repo"
    root.mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "probe@example.invalid"],
        ["git", "config", "user.name", "probe"],
        ["git", "remote", "add", "origin", "https://example.invalid/jedai/probe-repo.git"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    (root / "README.md").write_text("probe\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    p = subprocess.run(
        [sys.executable, "-c", CHILD, str(root), str(tmp / "memory.sqlite"), FIRST, SECOND],
        capture_output=True,
        text=True,
        timeout=900,
        cwd=tmp,  # neutral cwd: the memotron checkout's .env must not leak in (T1-14)
    )
    data = None
    for line in p.stdout.splitlines():
        if line.startswith("__R__"):
            data = json.loads(line[5:])
            break
    shutil.rmtree(tmp, ignore_errors=True)
    if data is None:
        print("  HARNESS ERROR\n" + (p.stdout[-300:] + p.stderr[-1200:]))
        return 2

    rows = data["has_state_rows"]
    print("  two contradicting compactions, rule-based only, one graph\n")
    print(f"  EPISODES stored: {data['episodes']}")
    for i, b in enumerate(data["episode_bodies"], 1):
        hit = [w for w in ("Postgres, not SQLite", "stay SQLite, not Postgres", "reversed") if w in b]
        print(f"    ep{i}: len={len(b)}  contains={hit or 'NEITHER SUMMARY'}")
    print(f"  HAS_STATE before run_due_dreams: {len(data['before_dreams'])} row(s)")
    for r in data["before_dreams"]:
        print(f"    status={r['status']} observed_count={r['oc']} {r['fact']!r}")
    print(
        f"  HAS_STATE after  run_due_dreams: {len(data['after_dreams'])} row(s)"
        + (f"   [dream error: {data['dream_err']}]" if data.get("dream_err") else "")
    )
    for r in data["after_dreams"]:
        print(f"    status={r['status']} observed_count={r['oc']} {r['fact']!r}")
    print("  ALL relationships:")
    for r in data["all_rows"]:
        print(f"    {r['type']:10s} {r['status']!s:10s} {r['fact']!r}")
    print()
    print(f"  HAS_STATE rows: {len(rows)}   (all relationships: {data['total_rows']})\n")
    for i, r in enumerate(rows, 1):
        print(
            f"    [{i}] status={r['status']!r:12s} valid_to={r['valid_to']!r:22s} observed_count={r['observed_count']}"
        )
        print(f"        truth_key={r['truth_key']!r}")
        print(f"        fact({r['fact_len']}): {r['fact_head']!r}")

    # judge the POST-dream snapshot: pre-dream state is mid-flight, not a result
    statuses = [str(r["status"]).lower() for r in data["after_dreams"]]
    superseded = [s for s in statuses if "supersed" in s]
    active = [s for s in statuses if s == "active"]

    print("\n=== verdict ===")
    stale_before = len(data["before_dreams"]) == 1 and "reversed" not in data["before_dreams"][0]["fact"]
    if stale_before:
        print("  TIMING: before run_due_dreams the graph still asserted the OLD fact as active.")
        print("          Without a dream worker (B5) that state is permanent.")
    if len(data["after_dreams"]) == 1:
        print("  C: one row — the second write deduped/reinforced onto the first.")
        return 0
    if superseded:
        print(f"  A: {len(superseded)} row(s) SUPERSEDED, {len(active)} ACTIVE — the no-LLM path")
        print("     keeps a single rolling 'latest compaction' record. Bounded; no history.")
        return 0
    print(f"  B: {len(active)} rows, none superseded — verbatim blobs ACCUMULATE.")
    print("     Truth maintenance never applies to anything the no-LLM path writes;")
    print("     every compaction adds another unsupersedable row to the retrieval set.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
