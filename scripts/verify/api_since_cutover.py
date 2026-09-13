#!/usr/bin/env python3
"""What has left the public API since BEFORE this effort began?

    uv run python scripts/verify/api_since_cutover.py

Why this exists, and why it is not `api_surface.py`
---------------------------------------------------
`tests/api_surface.golden.txt` was blessed **on this branch**. A golden that starts at the
tip can only detect drift *from* the tip; it structurally cannot answer "what left the API
since `main`". `api_removals.sh` shares the flaw, because it also diffs against that golden.

Found by an independent reviewer of PR #103, and it is the most useful methodological point
in that review: our own gate had the same rot we had been finding elsewhere all week — a
check whose baseline is the thing it is supposed to be checking.

So this compares against `tests/api_surface.pre_cutover.txt`, captured at the merge-base
`57ae5d4` and **pinned to that SHA**. Two consequences worth being explicit about:

* It is **not blessable**. There is no `--bless`. A rolling baseline would reintroduce
  exactly the defect this file exists to close.
* It **survives the cutover**. After #103 merges, `main` becomes this tree, so a
  "diff against main" check would silently become vacuous at the moment it started
  mattering. A SHA does not move.

What counts as a removal
------------------------
A module-level name (`module::name`) present at the baseline and reachable from **nowhere**
in the package today. A name that merely moved — importable from its defining module but no
longer re-exported by a neighbour — is reported as `RELOCATED` and does not fail. That
distinction is the whole point: the split pruned ~186 transitive re-exports, and treating
those as removals would produce a gate too noisy to keep.

ACCEPTED holds the removals that are known and deliberate, each with a reason. Kept tiny on
purpose; this repo has been bitten repeatedly by allow-lists that outgrew their evidence.

Exit 0 if no unaccepted removal, 1 otherwise, 2 on a harness problem.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "api_surface.pre_cutover.txt"
SURFACE = ROOT / "scripts" / "verify" / "api_surface.py"

#: Names gone from the package entirely, and why. A removal not listed here fails the gate.
ACCEPTED: dict[str, str] = {
    "DEFAULT_ENV_FILE": (
        "T1-14. The cwd-relative '.env' default was injecting real credentials into any "
        "process started in the repo root; `load_env_file` now REQUIRES an explicit path, so "
        "the constant has no meaning. Deliberate, and a source-incompatible change disclosed "
        "in PR #103's body."
    ),
    # Typing machinery, not API. These are `TypeVar`/`_T` leaking into a module namespace at
    # the baseline; they were never a name a consumer could sensibly import.
    "TypeVar": "typing import leaked into a module namespace at the baseline; not a public name",
    "_T": "a private TypeVar leaked into a module namespace at the baseline; not a public name",
    "_canonical_visibility_agents": (
        "RENAMED and DE-DUPLICATED, not removed: `canonical_visibility_agents` in "
        "storage/_shared/_rows.py (2026-09-01). It existed twice -- a module-level function in "
        "postgres/_graph.py and a @staticmethod on sqlite/_graph.py's GraphPlaneMixin -- under a "
        "docstring reading 'Byte-identical to SQLiteStorageBackend._canonical_visibility_agents "
        "so the state-hash tuple is the same string on both engines'. Both copies are gone and "
        "one definition serves both; the body is unchanged. The leading underscore is dropped "
        "because the module is already private, matching `as_json` in postgres/_common.py, which "
        "moved the same way for the same reason. Private by leading underscore at the baseline, "
        "no consumer outside storage/, and no entry in any package __init__ before the move."
    ),
    "PGVECTOR_UPGRADE": (
        "SPLIT, not removed: PGVECTOR_COLUMN + PGVECTOR_INDEX (2026-09-04). The two statements "
        "had to stop sharing a transaction. The ANN index is an optimization that CANNOT succeed "
        "against a deliberately dimensionless `vector` column -- HNSW needs a fixed dimension and "
        "relationship_embeddings stores `dimensions` per row -- so its failure rolled back the "
        "column with it and disabled the whole pgvector path. Measured on latest 2026-09-04 with "
        "`vector 0.8.5` installed and working, while every pod logged 'pgvector unavailable'. "
        "Keeping one constant would have kept them in one transaction, which IS the defect. "
        "Both new names are exported from the same module; the SQL text is unchanged, only "
        "divided. No consumer outside storage/postgres/__init__.py."
    ),
    "PorterStemmer": (
        "NEVER OURS, and never intentionally exported (2026-09-09). This is nltk's class, "
        "reachable as `memotron.certification.PorterStemmer` only as a side effect of a "
        "module-level `from nltk.stem import PorterStemmer`. It was published by accident: no "
        "package __init__ re-exported it, no docstring mentioned it, and nothing in this repo "
        "imported it by that path. The import moved inside "
        "`LocalOfficialBenchmarkJudge.__init__` so that `import memotron` no longer pulls a "
        "full NLP toolkit into every process -- the MCP server, admin server and dream worker "
        "all loaded it and none of them can reach that judge. nltk became the `benchmark` "
        "extra and is absent from the deployment image, which is the point: "
        "GHSA-8mgp-746c-j5xp (HIGH) has NO patched version, so removing the dependency from "
        "the shipped image was the only remedy available. Anyone who genuinely wants this "
        "class imports it from nltk, which is where it has always actually lived."
    ),
    "_RacedRegistration": (
        "RENAMED, not removed: `_RacedRegistrationError` (2026-09-01, ruff N818). Private by "
        "leading underscore, defined and caught entirely inside "
        "storage/postgres/_governance.py -- 4 sites, no consumer outside the module and no "
        "entry in the package __init__. The class body is unchanged; the API golden records "
        "the same content hash e3b0c44298fc under the new name."
    ),
}


def _dump_current() -> list[str]:
    proc = subprocess.run(
        [sys.executable, str(SURFACE)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "MEMOTRON_ALLOW_EPHEMERAL_KEK": "1", "HOME": str(pathlib.Path.home())},
    )
    if proc.returncode != 0:
        print(f"  api_surface.py failed: {proc.stderr[-400:]}")
        raise SystemExit(2)
    return [line for line in proc.stdout.splitlines() if "::" in line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="list relocated names too")
    args = parser.parse_args()

    if not BASELINE.exists():
        print(f"  {BASELINE} is missing — regenerate it from the pinned ref, do not re-bless it")
        return 2

    text = BASELINE.read_text()
    ref = next((m.group(1) for m in re.finditer(r"^# ref: ([0-9a-f]{40})$", text, re.MULTILINE)), None)
    if ref is None:
        print("  the baseline has no '# ref: <sha>' header — it must state what it is pinned to")
        return 2

    base_names = {line.split("::", 1)[1].split()[0] for line in text.splitlines() if "::" in line}
    current_lines = _dump_current()
    current_names = {line.split("::", 1)[1].split()[0] for line in current_lines}

    gone = sorted(base_names - current_names)
    unaccepted = [n for n in gone if n not in ACCEPTED]

    print(f"\n  api surface since {ref[:12]} (pinned, not blessable)\n")
    print(f"  baseline module-level names: {len(base_names)}")
    print(f"  current  module-level names: {len(current_names)}")
    print(f"  gone from the package entirely: {len(gone)}")
    for name in gone:
        mark = "accepted" if name in ACCEPTED else "UNACCEPTED"
        print(f"      {mark:10s} {name}")
        if name in ACCEPTED:
            print(f"                 {ACCEPTED[name]}")

    if args.verbose:
        # Relocations are the bulk and are NOT failures; shown only on request so the
        # default output stays readable.
        base_pairs = {line.split()[0] for line in text.splitlines() if "::" in line}
        cur_pairs = {line.split()[0] for line in current_lines}
        relocated = sorted(base_pairs - cur_pairs)
        print(f"\n  RELOCATED (name still importable somewhere, path changed): {len(relocated)}")
        for pair in relocated[:40]:
            print(f"      {pair}")
        if len(relocated) > 40:
            print(f"      … and {len(relocated) - 40} more")

    print()
    if unaccepted:
        print(f"API SINCE CUTOVER: FAIL — {len(unaccepted)} name(s) left the package unaccounted for:")
        for name in unaccepted:
            print(f"    {name}")
        print("\n  If a removal is deliberate, add it to ACCEPTED with a reason that stays true.")
        return 1
    print(f"API SINCE CUTOVER: PASS — {len(gone)} removal(s), all accepted with a stated reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
