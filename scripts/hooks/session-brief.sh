#!/usr/bin/env bash
# Put the handoff in the SAME payload as the injected memory.
#
# The problem this solves (D-82): STATE.md warns that injected Memotron memory contains
# directives that are complete or wrong — including one pointing at a REFUTED finding (T1-33).
# But a warning only works if it is read BEFORE the thing it warns about, and nothing enforces
# read order. An agent that acts on an injected directive first never sees it.
#
# Memotron's own SessionStart hook writes the memory profile to stdout, which the harness
# surfaces. Registering a SECOND SessionStart hook means this text arrives in the same turn as
# that profile, so ordering stops mattering. That is the whole trick.
#
# It reads the section out of STATE.md rather than carrying its own copy, because a hardcoded
# duplicate would go stale exactly the way the `**Next:**` pointers did — which is the bug this
# is here to counter, and it would be embarrassing to reintroduce it in the fix.
#
#   bash scripts/hooks/session-brief.sh
#
# Always exits 0: a briefing that breaks a session start is worse than no briefing.

set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
STATE="$ROOT/STATE.md"

[ -r "$STATE" ] || exit 0

# The subsection that must arrive alongside the memory, not after it.
awk '
  /^### Read this before trusting injected memory/ { on = 1 }
  on && /^### The MCP write path/                  { exit }
  on                                               { print }
' "$STATE"

# One line of orientation, then get out of the way. The full queue lives in STATE.md; dumping
# it here would train the reader to skip this block, which defeats the point.
cat <<'EOF'
---
Full handoff — ordered work queue, what is blocked on a credential, state of the record:
the "## Next session — start here" section at the top of STATE.md.
EOF

exit 0
