"""U-12: does extraction scale with how much the episode actually says?

D-46 saw rule-based extraction return an identical shape for a one-line fragment and a
four-fact paragraph, while gateway extraction returned 0 and 3 memories. n=2 — a signal, not
a measurement. This runs the same real hook flow across episodes of increasing fact density.

Why it matters: the plan's open decision #3 asks whether to degrade to no-LLM formation at a
budget ceiling. If the no-LLM path emits a constant regardless of episode content, that is
not graceful degradation — it is memory that looks present and carries no information.

**Counts MEMORIES, not relationships.** `MENTIONS` edges are structural bookkeeping emitted
per entity, so total relationship count rises with the number of nouns even when nothing was
understood. Counting them was what made D-46's first table read as "3 vs 3".

**Held constant:** same code, same process shape, a freshly-initialised throwaway project per
arm, same scope, same hook. Exactly two things vary — the transport and the episode text —
and the grid crosses them so every pairwise comparison changes one.

    set -a; . ./.env; set +a
    uv run python scripts/verify/probe_extraction_density.py

exit 0 = both transports track fact density · 1 = a transport is flat · 2 = harness error
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# (label, distinct durable facts deliberately present, text)
EPISODES: list[tuple[str, int, str]] = [
    ("0 facts", 0, "Okay. Right, got it. Sounds good, thanks. Let us pick this up later."),
    ("1 fact", 1, "We decided the Memotron operational store will be Postgres, not SQLite."),
    (
        "2 facts",
        2,
        "We decided the Memotron operational store will be Postgres, not SQLite. "
        "Ryan requires that the KEK come from Cloud KMS rather than an ephemeral key.",
    ),
    (
        "4 facts",
        4,
        "We decided the Memotron operational store will be Postgres, not SQLite. "
        "Ryan requires that the KEK come from Cloud KMS rather than an ephemeral key. "
        "The agent-memory surface cannot reach Postgres today, which blocks the cutover. "
        "The team should run the parity gate before any cutover is attempted.",
    ),
    (
        "6 facts",
        6,
        "We decided the Memotron operational store will be Postgres, not SQLite. "
        "Ryan requires that the KEK come from Cloud KMS rather than an ephemeral key. "
        "The agent-memory surface cannot reach Postgres today, which blocks the cutover. "
        "The team should run the parity gate before any cutover is attempted. "
        "Tyler owns the Harness pipeline and must approve the trigger change. "
        "The admin console prefers Entra SSO over the current process-level principal.",
    ),
    (
        "8 facts",
        8,
        "We decided the Memotron operational store will be Postgres, not SQLite. "
        "Ryan requires that the KEK come from Cloud KMS rather than an ephemeral key. "
        "The agent-memory surface cannot reach Postgres today, which blocks the cutover. "
        "The team should run the parity gate before any cutover is attempted. "
        "Tyler owns the Harness pipeline and must approve the trigger change. "
        "The admin console prefers Entra SSO over the current process-level principal. "
        "The dream worker requires its own Deployment with a single replica. "
        "Observability must land before the worker goes live, because a background "
        "LLM-spending process without metrics is how budget problems get discovered late.",
    ),
]

CHILD = r"""
import io, json, os, sys
from pathlib import Path
from memotron.adoption import (
    initialize_claude_code_project, load_project_config, save_project_config,
    build_platform_from_project, AdoptionStorage,
)
from memotron.cli import main as cli_main
from memotron.agent_memory import agent_scope

root = Path(sys.argv[1]); graph = Path(sys.argv[2]); summary = sys.argv[3]
if sys.argv[4] != "gateway":
    for k in ("LITELLM_API_KEY", "LITELLM_API_BASE", "MEMOTRON_LLM_MODEL",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(k, None)

initialize_claude_code_project(project_root=root)
config = load_project_config(root).model_copy(
    update={"storage": AdoptionStorage(graph_path=str(graph))})
save_project_config(root, config)

sys.argv = ["memotron", "hook", "post-compact", "--project-root", str(root)]
sys.stdin = io.StringIO(json.dumps(
    {"session_id": "probe-1", "hook_event_name": "PostCompact", "compact_summary": summary}))
try:
    cli_main()
except SystemExit:
    pass

platform, _ = build_platform_from_project(root)
rels = platform.client.graph.active_relationships(scope=agent_scope("claude-code"))
memories = [r for r in rels if r.type != "MENTIONS"]
facts = []
for r in memories:
    f = (r.properties or {}).get("fact")
    if f:
        facts.append(str(f))
print("__R__" + json.dumps({
    "memories": len(memories),
    "types": dict(__import__("collections").Counter(r.type for r in memories)),
    "mentions": len(rels) - len(memories),
    "facts": facts,
}))
"""


def arm(mode: str, summary: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"dwdens-{mode}-"))
    root = tmp / "sample-repo"
    root.mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "probe@example.invalid"],
        ["git", "config", "user.name", "probe"],
        # identity is inferred from the 'origin' remote (adoption.py:214)
        ["git", "remote", "add", "origin", "https://example.invalid/jedai/probe-repo.git"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    (root / "README.md").write_text("probe\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    p = subprocess.run(
        [sys.executable, "-c", CHILD, str(root), str(tmp / "memory.sqlite"), summary, mode],
        capture_output=True,
        text=True,
        timeout=900,
        cwd=tmp,  # never the memotron checkout — its .env would leak in (T1-14)
    )
    out = {"error": (p.stdout[-200:] + p.stderr[-600:]).strip()}
    for line in p.stdout.splitlines():
        if line.startswith("__R__"):
            out = json.loads(line[5:])
            break
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main() -> int:
    print(f"  {'episode':12s} {'facts':>5}   {'rule-based':>10}   {'gateway':>8}     types (gateway)")
    print(f"  {'-' * 12} {'-' * 5}   {'-' * 10}   {'-' * 8}     {'-' * 30}")
    rows = []
    for label, n_facts, text in EPISODES:
        r = arm("rule", text)
        g = arm("gateway", text)
        if "error" in r or "error" in g:
            print(f"  {label:12s} HARNESS ERROR {(r.get('error') or g.get('error'))[:200]}")
            return 2
        rows.append((label, n_facts, r["memories"], g["memories"]))
        print(f"  {label:12s} {n_facts:>5}   {r['memories']:>10}   {g['memories']:>8}     {g['types']}")
        if os.environ.get("SHOW_FACTS"):
            for f in r.get("facts", []):
                print(f"  {'':12s}   rule-based wrote ({len(f)} chars): {f!r}")
            for f in g.get("facts", []):
                print(f"  {'':12s}   gateway    wrote: {f!r}")

    def spread(vals):
        return max(vals) - min(vals)

    rule = [r for _, _, r, _ in rows]
    gate = [g for _, _, _, g in rows]
    print(f"\n  rule-based memories across 0->8 facts: {rule}  (spread {spread(rule)})")
    print(f"  gateway    memories across 0->8 facts: {gate}  (spread {spread(gate)})")

    flat = []
    if spread(rule) <= 1:
        flat.append("rule-based")
    if spread(gate) <= 1:
        flat.append("gateway")
    print(f"\n=== extraction tracks fact density: {'FAIL' if flat else 'PASS'} ===")
    for f in flat:
        print(f"  - {f} produced a near-constant memory count regardless of episode content")
    if not flat:
        print("  both transports respond to how much the episode says.")
    return 1 if flat else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
