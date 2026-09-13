"""Does a compaction episode actually form memory — and if not, what rejected it?

D-45 left this open: with a cwd `.env` routing extraction to the gateway (T1-14), a
post-compact episode produced `relationships = []`, where the deterministic rule-based path
produced the expected HAS_STATE. Three explanations:

  H1  the gateway call fails and the error is swallowed
  H2  the call succeeds and the model returns no candidates
  H3  candidates come back but strict validation rejects them all

H1 is ruled out at the transport level (`POST $LITELLM_API_BASE/chat/completions` -> 200).
H2 and H3 are distinguished HERE, by the system's own audit trail: a candidate rejected by
the schema emits a `CANDIDATE_SCHEMA_REJECTED` receipt naming the `CandidateViolation`. No
such receipt plus no relationships means the model returned nothing (H2); receipts present
means it returned candidates the validator refused (H3).

Runs the same flow as
`tests/test_adoption.py::test_post_compact_hook_persists_only_sanitized_continuity`, but
keeps the graph so the receipts can be read. **Holds one variable: whether the gateway
credential is in the environment.**

    set -a; . ./.env; set +a
    uv run python scripts/verify/probe_compaction_extraction.py

exit 0 = both arms formed memory · 1 = an arm formed none · 2 = harness error
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

SUMMARIES = {
    # the exact string from the adoption test — one line, and most of it is a secret and an
    # email address that sanitization strips BEFORE extraction ever sees them
    "short (the test's own)": ("Implement simple mode. password=do-not-store-this and notify engineer@example.com."),
    "realistic compaction summary": (
        "Session summary: we decided the Memotron operational store will be Postgres rather "
        "than SQLite for the latest environment. Ryan requires the KEK come from Cloud KMS, not "
        "an ephemeral per-process key. Blocker: the agent-memory surface cannot reach Postgres."
    ),
}
COMPACT_SUMMARY = SUMMARIES["realistic compaction summary"]

CHILD = r"""
import io, json, os, sys
from pathlib import Path
from memotron.adoption import (
    initialize_claude_code_project, load_project_config, save_project_config,
    build_platform_from_project,
)
from memotron.adoption import AdoptionStorage
from memotron.cli import main as cli_main
from memotron.agent_memory import agent_scope

root = Path(sys.argv[1]); graph = Path(sys.argv[2]); summary = sys.argv[3]
use_gateway = sys.argv[4] == "gateway"
if not use_gateway:
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
client = platform.client
from memotron.runtime import build_transports_from_env
transport, _d = build_transports_from_env()

eps = client.graph.episodes_for_scope("agent:claude-code")
rels = client.graph.active_relationships(scope=agent_scope("claude-code"))
try:
    receipts = list(client.graph.receipts())
except Exception:
    receipts = []
kinds = {}
for r in receipts:
    k = str(getattr(r, "kind", getattr(r, "receipt_type", "?")))
    kinds[k] = kinds.get(k, 0) + 1
print("__R__" + json.dumps({
    "transport": type(transport).__name__,
    "episodes": len(eps),
    "relationships": [r.type for r in rels],
    "receipt_kinds": kinds,
}))
"""


def arm(tag: str, mode: str, summary: str = COMPACT_SUMMARY) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"dwcompact-{mode}-"))
    root = tmp / "sample-repo"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "probe@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "probe"], cwd=root, check=True)
    # project identity is inferred from the 'origin' remote (adoption.py:214)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/jedai/probe-repo.git"], cwd=root, check=True
    )
    (root / "README.md").write_text("probe\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    p = subprocess.run(
        [sys.executable, "-c", CHILD, str(root), str(tmp / "memory.sqlite"), summary, mode],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=tmp,  # NOT the memotron checkout: keeps its .env out of scope (T1-14)
    )
    for line in p.stdout.splitlines():
        if line.startswith("__R__"):
            out = json.loads(line[5:])
            shutil.rmtree(tmp, ignore_errors=True)
            return out
    shutil.rmtree(tmp, ignore_errors=True)
    return {"error": (p.stdout[-300:] + "\n" + p.stderr[-2000:]).strip()}


def main() -> int:
    print(f"  gateway credential present in this shell: {bool(os.environ.get('LITELLM_API_KEY'))}\n")
    results = {}
    pairs = [
        (f"{stag} | {tag}", mode, text)
        for stag, text in SUMMARIES.items()
        for tag, mode in (("rule-based", "rule"), ("gateway", "gateway"))
    ]
    for tag, mode, text in pairs:
        r = arm(tag, mode, text)
        results[tag] = r
        if "error" in r:
            print(f"  {tag:44s} HARNESS ERROR\n{r['error'][-500:]}")
            continue
        rels = r["relationships"]
        print(f"  {tag:44s} rel={len(rels):<2} {dict(Counter(rels))}")
        rejects = {k: v for k, v in r["receipt_kinds"].items() if "REJECT" in k.upper() or "QUARANTINE" in k.upper()}
        if rejects:
            print(f"  {'':44s} rejection receipts: {rejects}")

    bad = [t for t, r in results.items() if "error" in r or not r.get("relationships")]
    print(f"\n=== compaction forms memory: {'FAIL' if bad else 'PASS'} ===")
    for t in bad:
        print(f"  - {t} formed no relationships from a compaction summary")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
