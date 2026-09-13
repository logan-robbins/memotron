#!/usr/bin/env bash
# Clean-room reproduction: are the Tier-0 findings real, or artefacts of our machine?
#
# Two of this week's findings were retracted because our own environment faked them -- a
# `.env` in the working directory (D-45) and zero delay between hook calls (D-50). So
# "it reproduces on my laptop" is not evidence. This script reproduces the Tier-0 findings
# in an environment that shares nothing with the development worktree:
#
#   * a FRESH git clone into a temp dir -- not this worktree
#   * NO .env, NO .memotron/, NO .claude/ hooks (the first two are gitignored and so
#     cannot follow the clone; .claude/ is deleted explicitly)
#   * every MEMOTRON_* / LITELLM_* / ANTHROPIC_* / OPENAI_* variable stripped
#   * a DEDICATED Postgres container on a non-default port, created and destroyed here
#
# The subtlety that makes this meaningful: the fixes are COMMITTED on `takeover`, so a plain
# clone would show every gate green and prove nothing. Instead each gate runs twice --
# against origin/main's src/ (expect RED, the defect reproduces) and against takeover's src/
# (expect GREEN, the fix works) -- with only src/ swapped between the two. The probes
# themselves never change, so the source tree is the single variable.
#
#   bash scripts/verify/cleanroom.sh
#
# exit 0 = every finding reproduced RED on main and GREEN with the fix · 1 = a finding did
# not behave as recorded (investigate before presenting it) · 2 = harness error

set -uo pipefail

REPO_URL="${REPO_URL:-git@github.disney.com:jedai/memotron.git}"
BRANCH="${BRANCH:-takeover}"
# Pinned by SHA, not by ref name. A ref resolves against whatever repo we cloned FROM: a
# clone of a local worktree gets that worktree's `main`, which may be stale (it was --
# 47790d4, pre-#44, which silently reverted src/ to the wrong tree and produced harness
# errors that looked like findings). A SHA means the same thing everywhere.
BASELINE="${BASELINE:-f28e95c}"   # origin/main, the PR #44 merge
# A free ephemeral port, chosen at run time. Deliberately not the dev stack's 55432, and
# not hardcoded either -- a fixed port collides with whatever else the machine is running,
# which is itself a way for the host environment to contaminate the result.
PG_PORT="${PG_PORT:-$(python3 -c "import socket;s=socket.socket();s.bind((\"\",0));print(s.getsockname()[1]);s.close()")}"
PG_NAME="dwcleanroom"
WORK="$(mktemp -d -t dwcleanroom)"

cleanup() {
  docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
  [ -n "${KEEP:-}" ] || rm -rf "$WORK"
}
trap cleanup EXIT

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "clean room: $WORK"

# ---------------------------------------------------------------- fresh clone
git clone --quiet --branch "$BRANCH" "$REPO_URL" "$WORK/repo" 2>&1 | tail -2 || {
  echo "  clone failed -- is the remote reachable?"; exit 2; }
cd "$WORK/repo" || exit 2

# Hooks are committed on this branch; remove them so nothing runs on our behalf.
rm -rf .claude
echo "  cloned $BRANCH @ $(git rev-parse --short HEAD)"
echo "  contaminants present?  .env=$([ -e .env ] && echo YES || echo no)" \
     " .memotron=$([ -e .memotron ] && echo YES || echo no)" \
     " .claude=$([ -e .claude ] && echo YES || echo no)"

# ---------------------------------------------------------------- fresh postgres
say "starting a dedicated postgres on :$PG_PORT"
docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
docker run -d --name "$PG_NAME" -e POSTGRES_USER=dw -e POSTGRES_PASSWORD=cleanroom \
  -e POSTGRES_DB=postgres -p "$PG_PORT:5432" postgres:16 >/dev/null 2>&1 || {
  echo "  could not start postgres"; exit 2; }
for _ in $(seq 1 120); do
  docker exec "$PG_NAME" pg_isready -U dw >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$PG_NAME" pg_isready -U dw >/dev/null 2>&1 || {
  echo "  postgres never became ready; container log:"; docker logs "$PG_NAME" 2>&1 | tail -5; exit 2; }
DSN="host=127.0.0.1 port=$PG_PORT user=dw password=cleanroom dbname=cleanroom"

fresh_db() {
  docker exec "$PG_NAME" psql -U dw -d postgres -c "DROP DATABASE IF EXISTS cleanroom;" >/dev/null 2>&1
  docker exec "$PG_NAME" psql -U dw -d postgres \
    -c "CREATE DATABASE cleanroom LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0 ENCODING 'UTF8';" >/dev/null 2>&1
}

say "installing dependencies"
uv sync --extra postgres --quiet 2>&1 | tail -2

# ---------------------------------------------------------------- the runner
# Every Memotron-influencing variable is stripped per invocation, so nothing from the
# calling shell can reach the code under test.
#
# ALLOW_EPHEMERAL_KEK is the one variable deliberately SET rather than stripped. It is a
# no-op on the baseline (no guard exists there) and REQUIRED on the fixed tree, where the
# fail-closed guard refuses to build a Postgres backend without it -- every probe seals into
# a throwaway database inside one process, which is exactly what the opt-out is for.
# Stripping it made the fixed tree exit 2 on every Postgres gate, which read as "did not
# reproduce" when it was actually the guard doing its job. The guard itself is still
# exercised, by test_postgres_backend_refuses_an_ephemeral_kek, which unsets it internally.
run_gate() {  # run_gate <label> <command...>
  local label="$1"; shift
  fresh_db
  env -u MEMOTRON_GRAPH_PATH -u MEMOTRON_OPERATIONAL_STORE_DSN \
      -u MEMOTRON_PROJECT_ID -u MEMOTRON_MODE -u MEMOTRON_LLM_MODEL \
      -u MEMOTRON_DREAM_AGENT_MODEL \
      -u LITELLM_API_KEY -u LITELLM_API_BASE -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
      MEMOTRON_ALLOW_EPHEMERAL_KEK=1 \
      "$@" >"$WORK/$label.log" 2>&1
  echo $?
}

declare -a ROWS=()
record() { ROWS+=("$1|$2|$3|$4"); }

gates_for_tree() {  # gates_for_tree <phase: main|fixed>
  local phase="$1"
  record "$phase" "T0-4 parity_postgres" \
    "$(run_gate "$phase-t04" uv run python scripts/verify/parity_postgres.py "$DSN")" ""
  record "$phase" "T0-5 probe_storage_wiring" \
    "$(run_gate "$phase-t05" uv run python scripts/verify/probe_storage_wiring.py "$DSN")" ""
  record "$phase" "T0-2 probe_kek" \
    "$(run_gate "$phase-t02" uv run python scripts/verify/probe_kek.py "$DSN")" ""
  record "$phase" "T0-7 probe_tamper_detection" \
    "$(run_gate "$phase-t07" uv run python scripts/verify/probe_tamper_detection.py "$DSN")" ""
  record "$phase" "regression tests" \
    "$(MEMOTRON_TEST_POSTGRES_DSN="$DSN" run_gate "$phase-pytest" uv run pytest -q \
        tests/test_scoped_read_parity.py tests/test_operational_store_wiring.py)" ""
}

# ---------------------------------------------------------------- RED: main's source
say "RED phase -- src/ reverted to $BASELINE (the unfixed baseline)"
git checkout --quiet "$BASELINE" -- src/
echo "  src/ now at $(git rev-parse --short "$BASELINE"); probes unchanged"
gates_for_tree main

# ---------------------------------------------------------------- GREEN: our source
say "GREEN phase -- src/ restored to $BRANCH (the fixes)"
git checkout --quiet HEAD -- src/
gates_for_tree fixed

# ---------------------------------------------------------------- verdict
say "results  (probes identical in both phases; only src/ differs)"
printf '  %-30s %-18s %s\n' "gate" "main (expect RED)" "fixed (expect GREEN)"
printf '  %-30s %-18s %s\n' "------------------------------" "-----------------" "--------------------"
FAIL=0
for gate in "T0-4 parity_postgres" "T0-5 probe_storage_wiring" "T0-2 probe_kek" \
            "T0-7 probe_tamper_detection" "regression tests"; do
  m=""; f=""
  for row in "${ROWS[@]}"; do
    IFS='|' read -r ph g rc _ <<<"$row"
    [ "$g" = "$gate" ] && [ "$ph" = "main" ]  && m="$rc"
    [ "$g" = "$gate" ] && [ "$ph" = "fixed" ] && f="$rc"
  done
  verdict="reproduced"
  # T0-2 and T0-7 have no committed fix yet, so RED in both phases is the expected result.
  case "$gate" in
    "T0-2 probe_kek"|"T0-7 probe_tamper_detection")
      [ "$m" != "0" ] || { verdict="DID NOT REPRODUCE"; FAIL=1; } ;;
    *)
      { [ "$m" != "0" ] && [ "$f" = "0" ]; } || { verdict="UNEXPECTED"; FAIL=1; } ;;
  esac
  printf '  %-30s exit=%-13s exit=%-13s %s\n' "$gate" "$m" "$f" "$verdict"
done

say "clean-room verdict: $([ "$FAIL" = 0 ] && echo 'every finding behaved as recorded' || echo 'SOMETHING DID NOT REPRODUCE')"
echo "  logs: $WORK/*.log   (re-run with KEEP=1 to retain them)"
exit "$FAIL"
