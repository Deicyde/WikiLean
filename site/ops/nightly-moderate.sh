#!/bin/bash
# WikiLean nightly moderation — invoked by launchd (org.wikilean.moderate).
#
# Runs as the logged-in user so the Claude Max-plan subscription login (read by
# the `claude` CLI the agent SDK spawns) is available. launchd hands us a bare
# environment, so every path is absolute and PATH is set explicitly.
#
# Sequence: flush any checkpointed-but-unposted work from a prior failed run
# (free), drift-sweep (wp-update, zero agent tokens), then a bounded review
# batch (search-verified Agent 2). Limits/budget are env-overridable so the
# same script serves the smoke test and the production schedule.
set -uo pipefail

REPO="/Users/jack/Desktop/LEAN/WikiLean"
PY="$REPO/catalog/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export WIKILEAN_MATHLIB="/Users/jack/Desktop/LEAN/mathlib4"
export WIKILEAN_API_TOKEN="$(sed -n 's/^PIPELINE_TOKEN=//p' "$REPO/wiki/.dev.vars")"

# Force Max-subscription auth. launchd hands us a bare env (no key), but when
# this is launched interactively via run-now.sh it inherits ANTHROPIC_API_KEY
# from the user's profile — the SDK would then bill that (out-of-credits) API
# account and every agent call dies "Credit balance is too low" with 0 tokens.
# Scrub it so both launch paths use the Max login.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

# Tunables (production defaults; overridden in the smoke test).
WPUPDATE_LIMIT="${WIKILEAN_WPUPDATE_LIMIT:-300}"
REVIEW_LIMIT="${WIKILEAN_REVIEW_LIMIT:-15}"
CONCURRENCY="${WIKILEAN_CONCURRENCY:-2}"
BUDGET_TOKENS="${WIKILEAN_BUDGET_TOKENS:-700000}"

LOGDIR="$REPO/site/cache/cron"
mkdir -p "$LOGDIR"
TS="$(date +%Y%m%dT%H%M%S)"
LOG="$LOGDIR/moderate-$TS.log"

# Single-instance lock (macOS has no flock): atomic mkdir, with stale recovery
# after 4h in case a prior run was killed without cleaning up. A review batch
# should never exceed ~2-3h.
LOCKDIR="$LOGDIR/.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    rmdir "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null || { echo "[$TS] lock race — skipping" >>"$LOGDIR/skips.log"; exit 0; }
  else
    echo "[$TS] previous run still active — skipping" >>"$LOGDIR/skips.log"
    exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

cd "$REPO/site" || exit 1
{
  echo "=== WikiLean nightly moderation $TS ==="
  echo "PY=$PY  wp=$WPUPDATE_LIMIT review=$REVIEW_LIMIT conc=$CONCURRENCY budget=$BUDGET_TOKENS"
  echo
  echo "--- flush prior checkpoints (zero agent tokens) ---"
  "$PY" moderate.py flush || echo "(flush returned $?)"
  echo
  echo "--- drift sweep: wp-update (zero agent tokens) ---"
  "$PY" moderate.py wp-update --limit "$WPUPDATE_LIMIT" || echo "(wp-update returned $?)"
  echo
  echo "--- review batch (search-verified) ---"
  "$PY" moderate.py review --limit "$REVIEW_LIMIT" --concurrency "$CONCURRENCY" \
        --budget-tokens "$BUDGET_TOKENS" || echo "(review returned $?)"
  echo
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/moderate-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
