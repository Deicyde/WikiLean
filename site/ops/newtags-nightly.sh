#!/bin/bash
# WikiLean nightly NEW-article tagging — invoked by launchd (org.wikilean.newtags)
# at 03:10 local. RECURRING (unlike new-once.sh, it does NOT self-remove).
#
# Runs `moderate.py new` on a curated candidate list to fetch → Agent 1 → Agent 2
# (Mathlib) → render → CREATE each not-yet-in-D1 article via the bot-only PUT.
# The agent model is Sonnet by default (WIKILEAN_AGENT_MODEL) for higher Max
# throughput / more parallel agents than the opus-pinned review cohort.
#
# Runs as the logged-in user so the Claude Max-plan login is available. launchd
# hands a bare environment, so every path is absolute and PATH is explicit.
#
# COEXISTENCE with the 03:20 moderation job (org.wikilean.moderate): this job
# uses its OWN lock (.lock.newtags.d), so it does NOT block the 03:20 run — both
# may run and compete for the shared Max 5-hour window; the rate-limit retry
# below smooths that (each backs off on a 429/window-exhaust and resumes). To
# make them mutually exclusive instead (newtags-wins, moderation-skips), point
# LOCKDIR at "$LOGDIR/.lock.d" (the shared nightly lock).
set -uo pipefail

REPO="/Users/jack/Desktop/LEAN/WikiLean"
PY="$REPO/catalog/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export WIKILEAN_MATHLIB="/Users/jack/Desktop/LEAN/mathlib4"
export WIKILEAN_API_TOKEN="$(sed -n 's/^PIPELINE_TOKEN=//p' "$REPO/wiki/.dev.vars")"

# Force Max-subscription auth (see the moderate wrapper for the full rationale):
# an inherited ANTHROPIC_API_KEY would bill an out-of-credits API account and
# every agent call dies with 0 tokens. Scrub it so both launch paths use Max.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

# Editable tunables live in site/ops/nightly.env (sourced with ":=" so an env
# override still wins). Missing file → the inline defaults below.
[ -f "$REPO/site/ops/nightly.env" ] && . "$REPO/site/ops/nightly.env"

# Tunables (fallback defaults if nightly.env is absent / silent on these).
# NEWTAGS-only model var -> the WIKILEAN_AGENT_MODEL that batch_annotate reads,
# mapped ONLY inside this process so the shared nightly.env never flips the
# 03:20 moderation job off its pinned-opus cohort.
MODEL="${WIKILEAN_NEWTAGS_MODEL:-claude-sonnet-5}"; export WIKILEAN_AGENT_MODEL="$MODEL"
LIMIT="${WIKILEAN_NEWTAGS_LIMIT:-40}"
CONCURRENCY="${WIKILEAN_NEWTAGS_CONCURRENCY:-6}"
BUDGET="${WIKILEAN_NEWTAGS_BUDGET:-2000000}"
CANDS="${WIKILEAN_NEWTAGS_CANDS:-$REPO/site/data/new-candidates-cs.jsonl}"

LOGDIR="$REPO/site/cache/cron"
mkdir -p "$LOGDIR"
TS="$(date +%Y%m%dT%H%M%S)"
LOG="$LOGDIR/newtags-$TS.log"

# Retry an agent step across a Max-window reset. moderate.py exits 3 on a
# consecutive-window-exhaustion abort AND on an intentional token-budget stop;
# retry ONLY when the fresh log tail carries the Max rate-limit signature (never
# on a budget stop). Bounded so a stuck night can't run into the morning.
RETRY_SLEEP="${WIKILEAN_RETRY_SLEEP:-900}"
RETRY_MAX="${WIKILEAN_RETRY_MAX:-3}"
retry_on_ratelimit() {
  local n=0 rc
  while : ; do
    "$@"; rc=$?
    [ "$rc" -ne 3 ] && return "$rc"
    if ! tail -n 80 "$LOG" 2>/dev/null | grep -qiE "hit your limit|usage limit|resets [0-9]|rate_limited_429"; then
      return "$rc"   # exit 3 without the Max signature = intended budget stop
    fi
    n=$((n + 1))
    if [ "$n" -ge "$RETRY_MAX" ]; then
      echo "  (rate-limited: exhausted $n retries across the Max reset — leaving the rest for tomorrow)"
      return "$rc"
    fi
    echo "  (Max window exhausted; sleeping ${RETRY_SLEEP}s for the reset, then retry $n/$((RETRY_MAX - 1)))"
    sleep "$RETRY_SLEEP"
  done
}

# Single-instance lock — SEPARATE from the moderation lock so this job coexists
# with the 03:20 run (see the header). Atomic mkdir, 4h stale recovery.
LOCKDIR="$LOGDIR/.lock.newtags.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    rmdir "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null || { echo "[$TS] lock race — skipping newtags" >>"$LOGDIR/skips.log"; exit 0; }
  else
    echo "[$TS] previous newtags run still active — skipping" >>"$LOGDIR/skips.log"
    exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

cd "$REPO/site" || exit 1
{
  echo "=== WikiLean nightly NEW-article tagging $TS ==="
  echo "model=$MODEL  candidates=$CANDS  limit=$LIMIT  concurrency=$CONCURRENCY  budget=$BUDGET"
  echo
  echo "--- flush prior checkpoints (zero agent tokens) ---"
  "$PY" moderate.py flush || echo "(flush returned $?)"
  echo
  if [ ! -s "$CANDS" ]; then
    echo "(no candidate file at $CANDS — nothing to do)"
  else
    echo "--- annotate + create NEW articles (Sonnet, search-verified Mathlib) ---"
    retry_on_ratelimit "$PY" moderate.py new --from-file "$CANDS" \
          --limit "$LIMIT" --concurrency "$CONCURRENCY" --budget-tokens "$BUDGET" \
      || echo "(new returned $?)"
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 newtags logs.
ls -1t "$LOGDIR"/newtags-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
