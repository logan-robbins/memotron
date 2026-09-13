#!/usr/bin/env bash
# Every name that LEFT the API surface, complete.
#
# `api_surface.py --check` prints a unified diff for the human, and truncates it
# when the change is large. That is fine for reading and wrong for deciding: during
# the dreaming split a commit reported "3 removals" out of an actual 22, and the
# commit message repeated the truncated number. This prints the full set, computed
# by set difference rather than by reading a rendered diff.
set -euo pipefail
cd "$(dirname "$0")/../.."
MEMOTRON_ALLOW_EPHEMERAL_KEK=1 uv run --no-sync python scripts/verify/api_surface.py 2>/dev/null \
  | sort > /tmp/api_actual.$$
comm -23 <(sort tests/api_surface.golden.txt) /tmp/api_actual.$$ | grep '::' || echo "(no removals)"
rm -f /tmp/api_actual.$$
