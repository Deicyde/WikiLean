#!/bin/bash
# The Bridge Experiment — Tier 1a campaign launcher (docs/research/BRIDGE-EXPERIMENT.md).
#
# One command once `claude` CLI auth is fresh:
#   bench/run_campaign.sh dev     # 5 arms x 30-task dev split (the smoke)
#   bench/run_campaign.sh eval    # 5 arms x 341-task eval split (the campaign)
#
# Everything is resumable: re-running skips completed tasks. Arms run
# sequentially (each already fans out --concurrency tasks); the local v3 worker
# must be up for arm D (bench/arms/local_worker.mts — checked below).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SPLIT="${1:-dev}"
MODEL="${BRIDGE_MODEL:-claude-haiku-4-5-20251001}"
CONC="${BRIDGE_CONCURRENCY:-6}"
export WIKIBRAIN_MCP_URL="${WIKIBRAIN_MCP_URL:-http://localhost:8790/mcp}"

# Max-auth preflight: a single trivial CLI call. If this 401s, STOP — every run
# would fail the same way (re-authenticate by running `claude` interactively).
if ! env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
     claude -p "reply with exactly: ok" --model "$MODEL" 2>&1 | grep -qi "ok"; then
  echo "ABORT: claude CLI auth check failed (expired OAuth?). Run \`claude\` interactively once, then retry."
  exit 1
fi

# arm D's server must answer JSON-RPC before we burn tokens on the other arms
if ! curl -sf -X POST "$WIKIBRAIN_MCP_URL" -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"campaign","version":"0"}}}' >/dev/null; then
  echo "ABORT: no Wikibrain MCP at $WIKIBRAIN_MCP_URL — start it:"
  echo "  cd wiki && npx tsx ../bench/arms/local_worker.mts 8790 &"
  exit 1
fi

echo "=== Bridge campaign: split=$SPLIT model=$MODEL concurrency=$CONC ==="
for arm in A B C D E; do
  echo; echo "=== arm $arm ==="
  python3 bench/run_bridge.py --arm "$arm" --split "$SPLIT" \
    --model "$MODEL" --concurrency "$CONC" || echo "(arm $arm exited $? — resumable, continuing)"
done

echo; echo "=== scoring (decl-existence + typecheck where available) ==="
python3 bench/score_bridge.py || true
echo
echo "Next: python3 bench/judge_bridge.py --arm <A..E>   (then --calibration 50)"
echo "      python3 bench/score_bridge.py                (final table + McNemar)"
