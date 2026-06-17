#!/bin/bash
# ONE-SHOT: annotate NEW articles (moderate.py new) and create them in D1.
# Fired by launchd at a scheduled time (e.g. ~when the Max window resets), in
# the FDA-enabled background context. Self-removes after running so it never
# recurs. Reads a pre-computed candidate list (slug-diff vs D1) via --from-file
# so it does NO run-time probing (which would 503-storm the Worker).
set -uo pipefail

REPO="/Users/jack/Desktop/LEAN/WikiLean"
PY="$REPO/catalog/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export WIKILEAN_MATHLIB="/Users/jack/Desktop/LEAN/mathlib4"
export WIKILEAN_API_TOKEN="$(sed -n 's/^PIPELINE_TOKEN=//p' "$REPO/wiki/.dev.vars")"

LIMIT="${WIKILEAN_NEW_LIMIT:-25}"
BUDGET="${WIKILEAN_BUDGET_TOKENS:-6000000}"
CANDS="$REPO/site/data/new-candidates.jsonl"

# Self-remove the plist FIRST: the job has fired, so it must never reload on a
# future login. (launchctl bootout at the end unloads the current instance.)
rm -f "$HOME/Library/LaunchAgents/org.wikilean.new-once.plist"

LOGDIR="$REPO/site/cache/cron"; mkdir -p "$LOGDIR"
TS="$(date +%Y%m%dT%H%M%S)"; LOG="$LOGDIR/new-$TS.log"

# Shared single-instance lock (with the nightly job): atomic mkdir, 4h stale.
LOCKDIR="$LOGDIR/.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    rmdir "$LOCKDIR" 2>/dev/null; mkdir "$LOCKDIR" 2>/dev/null || exit 0
  else echo "[$TS] another run active — skipping new-once" >>"$LOGDIR/skips.log"; exit 0; fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

cd "$REPO/site" || exit 1
{
  echo "=== WikiLean one-shot NEW-article annotation $TS ==="
  echo "candidates=$CANDS  limit=$LIMIT  budget=$BUDGET"
  echo "--- flush prior checkpoints (zero agent tokens) ---"
  "$PY" moderate.py flush || echo "(flush rc=$?)"
  echo "--- annotate + create NEW articles ---"
  "$PY" moderate.py new --from-file "$CANDS" --limit "$LIMIT" --budget-tokens "$BUDGET" || echo "(new rc=$?)"
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

launchctl bootout "gui/$(id -u)/org.wikilean.new-once" 2>/dev/null || true
