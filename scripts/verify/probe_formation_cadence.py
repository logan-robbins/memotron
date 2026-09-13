"""U-13: what makes a hook's episode form without an explicit dream drain?

D-48, observed and unexplained: hook 1's episode materialised with no explicit
`run_due_dreams()`; hook 2's did not. The hypothesis was the formation job's
`cadence_seconds=1` — the first hook triggered a due dream, the second fell inside the window.

This matters because it bounds T1-15. If a scope only forms when a dream is due, the window
between hooks is exactly how long a deployed scope can keep asserting a **superseded** fact
while labelling it `active`. If instead formation only ever happens on an explicit drain, then
without the worker (B5) it never happens at all.

**Held constant:** same project, same graph, same scope, rule-based transport, same episode
shape. The ONLY variable is the wall-clock gap between hooks. No explicit `run_due_dreams()`
is ever called — that is the point.

    uv run python scripts/verify/probe_formation_cadence.py [gap_seconds ...]   # default 0 3

exit 0 = a gap makes hooks form · 1 = gap makes no difference · 2 = harness error
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHILD = r"""
import io, json, os, sys, time
from pathlib import Path
from memotron.adoption import (
    initialize_claude_code_project, load_project_config, save_project_config,
    build_platform_from_project, AdoptionStorage,
)
from memotron.cli import main as cli_main
from memotron.agent_memory import agent_scope

root = Path(sys.argv[1]); graph = Path(sys.argv[2]); gap = float(sys.argv[3])

# rule-based only, so formation is deterministic and free
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

def counts():
    platform, _ = build_platform_from_project(root)
    g = platform.client.graph
    eps = g.episodes_for_scope("agent:claude-code")
    rows = [r for r in g.relationships() if r.type == "HAS_STATE"]
    return len(eps), len(rows)

SUMMARIES = [
    "Decision one: the operational store will be Postgres.",
    "Decision two: the KEK will come from Cloud KMS.",
    "Decision three: the dream worker gets its own Deployment.",
]

steps = []
for i, s in enumerate(SUMMARIES, 1):
    if i > 1 and gap > 0:
        time.sleep(gap)          # the only variable under test
    hook(f"s{i}", s)
    e, r = counts()
    steps.append({"hook": i, "episodes": e, "has_state": r})

print("__R__" + json.dumps({"gap": gap, "steps": steps}))
"""


def run(gap: float) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"dwcad-{gap}-"))
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
        [sys.executable, "-c", CHILD, str(root), str(tmp / "memory.sqlite"), str(gap)],
        capture_output=True,
        text=True,
        timeout=900,
        cwd=tmp,  # neutral cwd — the checkout's .env must not leak in (T1-14)
    )
    out = {"error": (p.stdout[-200:] + p.stderr[-900:]).strip()}
    for line in p.stdout.splitlines():
        if line.startswith("__R__"):
            out = json.loads(line[5:])
            break
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main() -> int:
    gaps = [float(a) for a in sys.argv[1:]] or [0.0, 3.0]
    results = []
    for gap in gaps:
        r = run(gap)
        if "error" in r:
            print(f"  gap={gap}s HARNESS ERROR\n{r['error'][-400:]}")
            return 2
        formed = [s["has_state"] for s in r["steps"]]
        eps = [s["episodes"] for s in r["steps"]]
        results.append((gap, eps, formed))
        print(f"  gap={gap:>4}s   episodes after each hook: {eps}   HAS_STATE rows after each hook: {formed}")

    print()
    for gap, eps, formed in results:
        stalled = eps[-1] - formed[-1]
        print(f"  gap={gap:>4}s  {eps[-1]} episode(s) stored, {formed[-1]} formed, {stalled} awaiting a dream pass")

    grew = [g for g, _, f in results if len(set(f)) > 1]
    print(f"\n=== a wall-clock gap changes formation: {'YES' if grew else 'NO'} ===")
    if grew:
        print(f"  formation advanced without an explicit drain at gap(s): {grew}")
        print("  => the cadence hypothesis holds; the gap bounds how long a scope can")
        print("     assert a superseded fact (T1-15).")
        return 0
    print("  every gap produced the same count — formation did NOT advance on wall clock.")
    print("  => without the worker (B5), hook episodes never form. T1-15 is unbounded.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
