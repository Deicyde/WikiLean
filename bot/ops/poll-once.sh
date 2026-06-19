#!/bin/bash
# WikiLean @[wikidata] batch poller — ONE event-driven tick, invoked by launchd
# (org.wikilean.poll) on a StartInterval. This is the robust, sleep/reboot-safe
# equivalent of `poll.py --watch`: launchd re-runs it each interval instead of a
# long-lived daemon that sleeps through laptop suspend.
#
# Each tick acts on the current batch PR's GitHub state (poll.py):
#   merged PR  -> open the next batch (open_batch.py: lake cache get + build + PR)
#   gate met   -> trim to greens (split, force-tip push), LLM-triage the recycle,
#                 post the reviewer table + a ready-to-merge comment
#   else       -> wait (cheap: 2 gh calls).
# UNSUPERVISED: no --no-open, so a merge auto-opens the next batch (Jack's call).
#
# launchd hands a bare env, so paths are absolute and PATH is explicit:
#   ~/.elan/bin   -> lake/elan (the open path builds touched modules)
#   /opt/homebrew -> gh, git, python
set -uo pipefail

REPO="/Users/jack/Desktop/LEAN/WikiLean"
PY="$REPO/catalog/.venv/bin/python3"
MATHLIB="/Users/jack/Desktop/LEAN/mathlib4"
export PATH="/Users/jack/.elan/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# publish_queue.py POSTs the recycled/unreviewed queue to the wiki /api/queue.
export WIKILEAN_API_TOKEN="$(sed -n 's/^PIPELINE_TOKEN=//p' "$REPO/wiki/.dev.vars")"

# Force Max-subscription auth for triage's `claude -p`: with ANTHROPIC_API_KEY in
# the env it would bill the out-of-credits API account and fail identically to
# the moderation SDK. launchd has no key, but scrub anyway for manual runs.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

LOGDIR="$HOME/Library/Logs/WikiLean"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/poll.log"

# Single-instance lock: a settle (LLM triage) or open (lake build) runs for
# minutes; never stack a second tick. atomic mkdir, 60m stale recovery.
LOCKDIR="$LOGDIR/.poll-lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
    rmdir "$LOCKDIR" 2>/dev/null; mkdir "$LOCKDIR" 2>/dev/null || exit 0
  else exit 0; fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

cd "$REPO/bot" || exit 1
{
  echo "=== poll tick $(date +%Y%m%dT%H%M%S) ==="
  "$PY" poll.py --mathlib "$MATHLIB" --apply || echo "(poll rc=$?)"
} >>"$LOG" 2>&1

# Cap the rolling log at ~4000 lines.
if [ "$(wc -l <"$LOG" 2>/dev/null || echo 0)" -gt 4000 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
