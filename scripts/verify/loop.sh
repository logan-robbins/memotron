#!/usr/bin/env bash
# RED(1)=no dream-agent facts | GREEN(0)=formed | 2=harness error. Secret stays in env.
# Seeds from the configured dogfood graph so tenant/agent/LLM setup is already done.
set -uo pipefail
DW=/Users/ryan.van.valkenburg.-nd/repos/jedai/worktrees/memotron-implementation
cd "$DW"
set -a; . ./.env; set +a
: "${LITELLM_API_KEY:?missing}"
rm -rf /tmp/dwloop; mkdir -p /tmp/dwloop
cp "$DW/.memotron/dogfood.sqlite"     /tmp/dwloop/graph.sqlite
cp "$DW/.memotron/dogfood.sqlite.kek" /tmp/dwloop/graph.sqlite.kek
export MEMOTRON_GRAPH_PATH=/tmp/dwloop/graph.sqlite
export MEMOTRON_PROJECT_ID=memotron-dogfood
export MEMOTRON_PROJECT_NAME="Memotron (dogfood)"
export MEMOTRON_MODE=simple
uv run python "$1"
rc=$?
case $rc in
  0) echo "  == GREEN: dreaming formed memory ==";;
  1) echo "  == RED: dreaming formed NO memory ==";;
  *) echo "  == HARNESS ERROR (rc=$rc) ==";;
esac
exit $rc
