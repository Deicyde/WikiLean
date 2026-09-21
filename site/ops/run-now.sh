#!/bin/bash
# Fire a moderation batch NOW — e.g. when `/usage` shows you ~30 min from your
# Max window reset with capacity to spare. Runs DETACHED (survives closing the
# terminal) in your interactive login context, so it uses your Max subscription
# and needs no Full Disk Access grant (that's only for the launchd path).
#
# Burns leftover capacity: reviews up to WIKILEAN_REVIEW_LIMIT articles (default
# 100 — the /api/work per-fetch cap, and as much as a window can realistically
# spend). The real limiter is window exhaustion: the runner aborts cleanly the
# moment the Max window is spent, so overshooting the limit is harmless. We also
# lift the nightly 700k-token budget cap to ~6M here (WIKILEAN_BUDGET_TOKENS) so
# it can't prematurely halt a deliberate near-reset burn. The single-instance
# lock in nightly-moderate.sh prevents overlap with the nightly run.
#
#   bash site/ops/run-now.sh                              # full burn (review 100)
#   WIKILEAN_REVIEW_LIMIT=25 bash site/ops/run-now.sh     # smaller burn
#
# Suggested shell alias (add the absolute path of this checkout to ~/.zshrc).
# then just: wlmod
set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)" || exit 1
# shellcheck disable=SC1091
. "$SCRIPT_DIR/nightly-runtime.sh"
wikilean_ops_init || exit 1

if [ "${WIKILEAN_OPS_PREFLIGHT_ONLY:-0}" = "1" ]; then
  wikilean_ops_print_check
  exit 0
fi

: "${WIKILEAN_REVIEW_LIMIT:=100}"; export WIKILEAN_REVIEW_LIMIT
: "${WIKILEAN_BUDGET_TOKENS:=6000000}"; export WIKILEAN_BUDGET_TOKENS
mkdir -p "$HOME/Library/Logs/WikiLean"

nohup /bin/bash "$SCRIPT_DIR/nightly-moderate.sh" >/dev/null 2>&1 &
PID=$!
echo "WikiLean moderation started (detached, pid $PID, review limit $WIKILEAN_REVIEW_LIMIT)."
echo "  watch:  tail -f $REPO/site/cache/cron/moderate-*.log"
echo "  stop:   kill $PID   (or it stops itself on window exhaustion / limit)"
