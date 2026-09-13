#!/usr/bin/env bash
# Does the PRODUCT still work -- built, installed, and run like a consumer would?
#
#   bash scripts/verify/sanity.sh [baseline-ref]     # default baseline: takeover
#
# Every other gate in this repo measures the SOURCE TREE. `pure_move` compares
# bytecode, the API golden enumerates symbols, mixin_dag walks the call graph,
# coverage_floors reads a JSON report, receipt_golden diffs a recorded stream. All
# of them can be green on a tree that does not build, does not install, or installs
# and then fails on first use -- because none of them ever starts the thing.
#
# That gap was real, not hypothetical: this script was written after ~100 commits of
# restructuring during which the product had never once been run from an installed
# wheel. It found nothing broken. That is the point -- an unrun check is not evidence,
# and "it was fine last time" is not either.
#
# It is deliberately NOT part of scripts/check.sh. It builds two wheels and creates
# two venvs (~60s), which is the wrong cost on every commit and the right cost at a
# milestone: before a push, after finishing a module split, before opening a PR.
#
# The A/B is the load-bearing half
# --------------------------------
# Running the refactored build alone proves almost nothing, because most of what it
# prints has no known-good value to compare against -- is `created=0` from a dream
# pass a regression or the documented no-worker behaviour? The only cheap way to
# answer that is to run the IDENTICAL script against a pre-refactor wheel and diff.
# A difference is a finding; identical output clears the change by a path completely
# independent of the test suite.
set -uo pipefail
cd "$(dirname "$0")/../.."

BASE_REF="${1:-takeover}"
TMP="${TMPDIR:-/tmp}/dw-sanity-$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
fail=0

build_and_install() {  # <label> <srcdir> <venv>
  uv build --wheel -o "$TMP/$1-dist" "$2" -q >/dev/null 2>&1 || { echo "  $1: BUILD FAILED"; return 1; }
  uv venv "$3" -q >/dev/null 2>&1
  uv pip install -q --python "$3/bin/python" "$TMP/$1-dist"/*.whl >/dev/null 2>&1 \
    || { echo "  $1: INSTALL FAILED"; return 1; }
}

say "building the working tree"
build_and_install now . "$TMP/venv-now" || exit 1
WHEEL=$(ls "$TMP"/now-dist/*.whl)
echo "  $(basename "$WHEEL")"

# Every split package must actually be IN the wheel. hatchling packages
# src/memotron wholesale, so this should hold -- but "should" is why it is checked.
"$TMP/venv-now/bin/python" - "$WHEEL" <<'PY'
import sys, zipfile, collections
names = zipfile.ZipFile(sys.argv[1]).namelist()
pkgs = collections.Counter(n.split("/")[1] for n in names
                           if n.startswith("memotron/") and n.count("/") > 1 and n.endswith(".py"))
for p, c in sorted(pkgs.items()):
    print(f"    memotron/{p}/  {c} modules")
missing = [p for p in ("dreaming", "config", "models", "storage") if p not in pkgs]
print(f"  missing subpackages: {missing or 'none'}")
sys.exit(1 if missing else 0)
PY
[ $? -ne 0 ] && fail=1

say "importing from site-packages (no src/ on the path)"
(cd "$TMP" && MEMOTRON_ALLOW_EPHEMERAL_KEK=1 "$TMP/venv-now/bin/python" - <<'PY'
import memotron, memotron.dreaming, memotron.config, memotron.models
import memotron.storage.sqlite, memotron.client, memotron.admin_server
assert "site-packages" in memotron.__file__, memotron.__file__
from memotron.dreaming import DreamEngine, node_identity_key, truth_identity, _ContentProtection
print(f"    DreamEngine composes {len(DreamEngine.__mro__) - 2} mixins")
# R-S4: a submodule that took __name__ would silently rename every log record.
assert memotron.dreaming._log.name == "memotron.dreaming", memotron.dreaming._log.name
print(f"    logger name preserved: {memotron.dreaming._log.name}")
print("    memotron.migration's imports resolve")
PY
) || fail=1

say "A/B against $BASE_REF"
rm -rf "$TMP/base-src" && mkdir -p "$TMP/base-src"
git archive "$BASE_REF" | tar -x -C "$TMP/base-src" || { echo "  cannot archive $BASE_REF"; exit 1; }
build_and_install base "$TMP/base-src" "$TMP/venv-base" || exit 1

cat > "$TMP/probe.py" <<'PY'
import asyncio, sys
from collections import Counter
from memotron import Memotron, MemoryScope, ScopeKind, EpisodeType

async def main():
    dw = Memotron(graph_path=sys.argv[1])
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="sanity")
    # Structured bodies on purpose. These episodes are read by the RULE-BASED
    # extractor -- a clean venv has no LLM transport -- and it needs the
    # subject/predicate/object/relationship_type form. Free English extracts to
    # nothing, which is what this probe used to feed it: `created=0` on both sides,
    # identical, PASS. The gate ran the product and was blind to whether the product
    # did anything. Measured 2026-08-30, same graph and config, only the body differs:
    #     "The operator prefers postgres..."      -> created=0
    #     "Memory: subject=...; predicate=..."    -> created=1
    for n, obj in (("ep1", "postgres for the operational store"),
                   ("ep2", "sqlite for the operational store")):
        await dw.add_episode(
            name=n, source=EpisodeType.MESSAGE, scope=scope,
            episode_body=(f"Memory: subject=The operator; predicate=prefers; object={obj}; "
                          "relationship_type=PREFERS; confidence=0.93"),
        )
    res = await dw.run_dream_job(job_name="formation-default")
    created = getattr(res, "created_relationships", 0)
    facts = lambda: [r for r in dw.graph.relationships_for_scope(scope.key) if r.type != "MENTIONS"]
    print(f"    dream        created={created} "
          f"facts={len(facts())} receipts={len(dw.graph.receipts.receipts_for_scope(scope.key))}")
    # Hard-fail rather than print. A/B alone cannot catch formation breaking, because
    # zero on both sides diffs clean -- which is exactly how this gate passed while
    # exercising nothing.
    if not created:
        print("    FORMATION PRODUCED NOTHING -- this probe is not exercising the product")
        sys.exit(1)
    await dw.add_memory(subject="the api", predicate="requires", object="a durable store",
                        relationship_type="REQUIRES", scope=scope)
    hits = await dw.search(query="durable store", scope=scope)
    rows = facts()
    print(f"    direct write facts={len(rows)} search_hits={len(hits)} "
          f"statuses={dict(Counter(str(r.properties.get('status')) for r in rows))}")
    print(f"    receipts     total={len(dw.graph.receipts.receipts_for_scope(scope.key))}")
    for mod, label in (("memotron.mcp_server", "governance"),
                       ("memotron.agent_memory_mcp", "agent-memory")):
        m = __import__(mod, fromlist=["mcp"])
        print(f"    mcp {label:12} {len(await m.mcp.list_tools())} tools")

asyncio.run(main())
PY

for side in base now; do
  rm -f "$TMP/$side.sqlite"
  echo "  --- $side ---"
  (cd "$TMP" && MEMOTRON_ALLOW_EPHEMERAL_KEK=1 \
      "$TMP/venv-$side/bin/python" "$TMP/probe.py" "$TMP/$side.sqlite") \
    > "$TMP/$side.out" 2>&1 || { echo "    RUN FAILED"; cat "$TMP/$side.out"; fail=1; }
  cat "$TMP/$side.out"
done

say "verdict"
EXPECTED=tests/sanity_expected_diff.txt
if diff -q "$TMP/base.out" "$TMP/now.out" >/dev/null 2>&1; then
  echo "  IDENTICAL to $BASE_REF -- the change is behaviour-neutral end to end"
else
  # Split the diff into lines this branch declares it changes on purpose, and lines
  # it does not. Without this the harness goes permanently red on the first
  # intentional change, and a permanently red check is one nobody reads.
  unexplained=0
  while IFS= read -r line; do
    case "$line" in
      "<"*|">"*) ;;
      *) continue ;;
    esac
    body=${line#? }
    # -f treats the expected file as the PATTERN list and the diff line as the
    # input. The other way round only matches when the diff line is a substring of
    # a declaration, which is never.
    if [ -f "$EXPECTED" ] && printf '%s\n' "$body" \
         | grep -qF -f <(grep -v '^#' "$EXPECTED" | grep -v '^[[:space:]]*$'); then
      echo "    expected  $line"
    else
      echo "    UNEXPECTED $line"
      unexplained=$((unexplained + 1))
    fi
  done < <(diff "$TMP/base.out" "$TMP/now.out")
  if [ "$unexplained" -eq 0 ]; then
    echo "  DIFFERS from $BASE_REF, and every difference is declared in $EXPECTED"
  else
    echo "  $unexplained UNDECLARED difference(s) from $BASE_REF."
    echo "  A difference is not automatically a defect -- but it is a finding, and it"
    echo "  needs an explanation, and a line in $EXPECTED, before this lands."
    fail=1
  fi
fi

[ "$fail" -eq 0 ] && echo "SANITY: PASS" || echo "SANITY: FAIL"
exit "$fail"
