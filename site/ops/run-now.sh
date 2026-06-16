#!/bin/bash
# Fire a moderation batch NOW — e.g. when `/usage` shows you ~30 min from your
# Max window reset with capacity to spare. Runs DETACHED (survives closing the
# terminal) in your interactive login context, so it uses your Max subscription
# and needs no Full Disk Access grant (that's only for the launchd path).
#
# Burns leftover capacity: reviews up to WIKILEAN_REVIEW_LIMIT articles (default
# 30 — higher than the nightly 15 since you're deliberately spending the tail of
# a window). The runner aborts cleanly the moment the window is exhausted, so
# overshooting the limit is harmless. The single-instance lock in
# nightly-moderate.sh prevents overlap with the nightly run.
#
#   bash site/ops/run-now.sh            # default burn (review 30)
#   WIKILEAN_REVIEW_LIMIT=60 bash site/ops/run-now.sh   # bigger burn
#
# Suggested shell alias (add to ~/.zshrc):
#   alias wlmod='~/Desktop/LEAN/WikiLean/site/ops/run-now.sh'
# then just: wlmod
set -uo pipefail

REPO="/Users/jack/Desktop/LEAN/WikiLean"
: "${WIKILEAN_REVIEW_LIMIT:=30}"; export WIKILEAN_REVIEW_LIMIT
mkdir -p "$HOME/Library/Logs/WikiLean"

nohup /bin/bash "$REPO/site/ops/nightly-moderate.sh" >/dev/null 2>&1 &
PID=$!
echo "WikiLean moderation started (detached, pid $PID, review limit $WIKILEAN_REVIEW_LIMIT)."
echo "  watch:  tail -f $REPO/site/cache/cron/moderate-*.log"
echo "  stop:   kill $PID   (or it stops itself on window exhaustion / limit)"
