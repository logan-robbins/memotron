#!/usr/bin/env bash
# The live lane: probes that make REAL calls, checked against a recorded baseline.
#
#   bash scripts/verify/live_lane.sh            # run and compare
#   bash scripts/verify/live_lane.sh --record   # rewrite the baseline (say why in the commit)
#
# WHY THIS EXISTS
# ---------------
# All twelve `check.sh` lanes are hermetic or static. None of them makes a real call — no
# gateway, no live Postgres audit, no second process. The probes that DO exist and are
# committed were run by hand, and their results were recorded as PROSE in the verification
# skill. Two of those recorded numbers turned out to be wrong on first contact:
#
#   core loop `15/15`   never true. AST-counted at introduction and today: 14 check() calls.
#   admin sweep 35      `GET OK=16 POST OK=4 HTTP503=15` accounted for 35 of the 53 routes the
#                       sweep discovers. RESOLVED 2026-09-01 (#140) and now in the baseline:
#                       those three buckets do not span the outcome space. HTTP400 and HTTP204
#                       had no bucket, so 14 of 53 outcomes were structurally uncountable. The
#                       "missing 18" was an artifact of the tally, not 18 unexercised routes --
#                       measured by sweeping a real server and reading every response.
#
# So the baseline is a FILE this script diffs, not a sentence someone remembers.
#
# EXPECTED EXIT, NOT ZERO
# -----------------------
# `probe_kek` is supposed to exit 1: it reproduces T0-2, which is filed and unfixed. A lane
# that demanded success would be red on arrival, and a lane that is always red gets muted.
# Drift in EITHER direction is reported, and an unexpected PASS is as interesting as a
# failure -- it means a defect was fixed and the record has not caught up.
#
# CADENCE
# -------
# Milestone-scoped, like `sanity.sh`, never per-commit: before a push that touches storage,
# formation or the audit plane; after a module lands; before a PR is marked ready. It needs
# credentials and a live Postgres, so it cannot be a CI lane today -- and pretending otherwise
# by skipping silently is the exact failure `check.sh` already refuses.
#
# exit 0 = every probe matched its baseline · 1 = drift · 2 = could not run
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2

BASELINE="tests/live_lane.baseline.tsv"
RECORD=0
[ "${1:-}" = "--record" ] && RECORD=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

say "live lane"
note "repo:   $(pwd)"
note "commit: $(git rev-parse --short HEAD 2>/dev/null || echo '(not a git checkout)')"

# ---------------------------------------------------------------- preconditions
# Reported loudly and never skipped silently. A live lane that quietly degrades to "nothing
# ran" is worse than no lane: it produces a green line that means nothing.
MISSING=0
if [ -z "${LITELLM_API_KEY:-}${OPENAI_API_KEY:-}" ]; then
  note "MISSING: no gateway key. probe_core_loop would fall back to RULE-BASED extraction and"
  note "         test nothing. Load the gitignored .env:  set -a; . ./.env; set +a"
  MISSING=1
fi
if [ -z "${MEMOTRON_TEST_POSTGRES_DSN:-}" ]; then
  note "MISSING: MEMOTRON_TEST_POSTGRES_DSN. Three of four probes need a live Postgres."
  MISSING=1
fi
# The two MCP probes talk to a DEPLOYED server, not to a DSN. They were opt-in for one
# commit and that was wrong: an un-exercised probe recorded at "expected exit 2" is a row
# that cannot fail, which is exactly the muting this file's header warns about.
#
# POINT THIS AT `latest`. It is the only environment with requireGatewayIdentity: true
# (values.yaml defaults it false and stage/load/preview/prod inherit that, #237), and
# probe_mcp_identity ABORTS at P0 against an unarmed target rather than measuring an
# unguarded server. Anything else here produces exit 2 by design, not a regression.
if [ -z "${MEMOTRON_PROBE_MCP_URL:-}" ] || [ -z "${MEMOTRON_PROBE_AGENTMEM_URL:-}" ]; then
  note "MISSING: MEMOTRON_PROBE_MCP_URL / MEMOTRON_PROBE_AGENTMEM_URL."
  note "         sweep_mcp and probe_mcp_identity sweep a DEPLOYED server; without a target"
  note "         they cannot run, and a lane that records that as a pass is worse than no lane."
  note "         export MEMOTRON_PROBE_MCP_URL=https://latest.jedai-memotron.wdprapps.disney.com/mcp"
  MISSING=1
fi
if [ -z "${MEMOTRON_PROBE_ALPHA_KEY:-}" ] || [ -z "${MEMOTRON_PROBE_BRAVO_KEY:-}" ]; then
  note "MISSING: MEMOTRON_PROBE_ALPHA_KEY / MEMOTRON_PROBE_BRAVO_KEY."
  note "         probe_mcp_identity needs TWO virtual keys bound to DIFFERENT tenants -- the"
  note "         same key twice cannot show a disagreement, which is the whole claim. Mint them"
  note "         on the -admin gateway host; the data-plane host refuses management routes."
  MISSING=1
fi

if [ "$MISSING" -eq 1 ]; then
  note ""
  note "REFUSING to run a partial live lane and call it a result."
  exit 2
fi
if [ ! -f "$BASELINE" ]; then
  note "MISSING: $BASELINE — run with --record to create it, and say why in the commit."
  exit 2
fi

DSN="$MEMOTRON_TEST_POSTGRES_DSN"
export MEMOTRON_ALLOW_EPHEMERAL_KEK=1

# probe -> argument. Kept here rather than derived: each probe's signature is its own, and a
# clever auto-detector would be one more thing that can be wrong about the tree.
probe_arg() {
  case "$1" in
    probe_core_loop) echo "" ;;
    # Self-hosting: starts its own admin server on a fresh graph, so it takes no DSN.
    probe_admin_surface) echo "" ;;
    # The two MCP probes talk to a DEPLOYED server, not a DSN. They are opt-in: without
    # the env vars below they exit 2, which the baseline records as "not exercised" --
    # NOT as a pass. Supply the vars and the expected exit becomes 0, at which point the
    # lane reports an UNEXPECTED PASS and asks you to update the baseline deliberately.
    # That is the same promotion path probe_kek has for #123.
    sweep_mcp)
      echo "${MEMOTRON_PROBE_MCP_URL:-} ${MEMOTRON_PROBE_AGENTMEM_URL:-}" ;;
    probe_mcp_identity)
      if [ -n "${MEMOTRON_PROBE_MCP_URL:-}" ] && [ -n "${MEMOTRON_PROBE_ALPHA_KEY:-}" ]; then
        echo "--url ${MEMOTRON_PROBE_MCP_URL} --alpha-key ${MEMOTRON_PROBE_ALPHA_KEY} --bravo-key ${MEMOTRON_PROBE_BRAVO_KEY:-}"
      else
        echo ""
      fi ;;
    *) echo "$DSN" ;;
  esac
}

declare -a DRIFT=()
declare -a LINES=()
say "probes"
while IFS=$'\t' read -r probe expected reason; do
  case "$probe" in ''|\#*) continue ;; esac
  script="scripts/verify/${probe}.py"
  if [ ! -f "$script" ]; then
    note "$(printf '%-24s MISSING %s' "$probe" "$script")"
    DRIFT+=("$probe: script does not exist")
    continue
  fi
  arg="$(probe_arg "$probe")"
  if [ -n "$arg" ]; then
    uv run --no-sync python "$script" "$arg" >/dev/null 2>&1
  else
    uv run --no-sync python "$script" >/dev/null 2>&1
  fi
  actual=$?
  LINES+=("$(printf '%s\t%s\t%s' "$probe" "$actual" "$reason")")
  if [ "$actual" = "$expected" ]; then
    note "$(printf '%-24s exit=%s  as recorded' "$probe" "$actual")"
  elif [ "$expected" != 0 ] && [ "$actual" = 0 ]; then
    note "$(printf '%-24s exit=%s  UNEXPECTED PASS (baseline %s)' "$probe" "$actual" "$expected")"
    DRIFT+=("$probe: expected $expected, got 0 — a filed defect appears FIXED. Update the baseline deliberately.")
  else
    note "$(printf '%-24s exit=%s  DRIFT (baseline %s)' "$probe" "$actual" "$expected")"
    DRIFT+=("$probe: expected $expected, got $actual")
  fi
done < "$BASELINE"

if [ "$RECORD" -eq 1 ]; then
  {
    head -n "$(grep -c '^#' "$BASELINE")" "$BASELINE"
    printf '%s\n' "${LINES[@]}"
  } > "$BASELINE.tmp" && mv "$BASELINE.tmp" "$BASELINE"
  say "recorded"
  note "$BASELINE rewritten from this run. Explain every changed expectation in the commit."
  exit 0
fi

say "verdict"
if [ ${#DRIFT[@]} -eq 0 ]; then
  note "every probe matched its recorded baseline"
  exit 0
fi
for d in "${DRIFT[@]}"; do note "$d"; done
note ""
note "If a change is intended, re-record with --record and say why. Do not edit the baseline"
note "by hand to make a run green — that is how a recorded number stops meaning anything."
exit 1
