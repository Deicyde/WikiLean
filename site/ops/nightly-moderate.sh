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

# Editable tunables live in site/ops/nightly.env (the one place to change the
# nightly rate/budgets). Sourced with ":=" so an env override still wins and the
# run-now.sh smoke-test env survives. Missing file → the inline defaults below.
[ -f "$REPO/site/ops/nightly.env" ] && . "$REPO/site/ops/nightly.env"

# Tunables (fallback defaults if nightly.env is absent; overridden in the smoke test).
WPUPDATE_LIMIT="${WIKILEAN_WPUPDATE_LIMIT:-300}"
REVIEW_LIMIT="${WIKILEAN_REVIEW_LIMIT:-15}"
CONCURRENCY="${WIKILEAN_CONCURRENCY:-2}"
BUDGET_TOKENS="${WIKILEAN_BUDGET_TOKENS:-700000}"
# Formalize backlog: Agent-2 the extracted (Agent-1-only) articles the manage/
# control plane surfaces, which the /api/work ladder can't see. Runs before the
# general review so the backlog gets first claim. Adjust the rate in nightly.env
# (WIKILEAN_FORMALIZE_LIMIT; 0 disables). NB: nightly spend ≈ FORMALIZE_BUDGET + BUDGET_TOKENS.
FORMALIZE_LIMIT="${WIKILEAN_FORMALIZE_LIMIT:-6}"
FORMALIZE_BUDGET="${WIKILEAN_FORMALIZE_BUDGET:-300000}"

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
  echo "--- refresh control plane: centrality + coverage + worklists (zero agent tokens) ---"
  # Offline by default (computes from disk). Set WIKILEAN_MANAGE_PULL=1 to pull
  # the live D1 annotation layer first (needs wrangler auth in this env — verify
  # before enabling, or it fails soft and refresh falls back to disk).
  MANAGE_PULL=""; [ "${WIKILEAN_MANAGE_PULL:-0}" = "1" ] && MANAGE_PULL="--pull"
  python3 "$REPO/manage/refresh.py" $MANAGE_PULL || echo "(manage refresh returned $?)"
  echo
  echo "--- flush prior checkpoints (zero agent tokens) ---"
  "$PY" moderate.py flush || echo "(flush returned $?)"
  echo
  echo "--- drift sweep: wp-update (zero agent tokens) ---"
  "$PY" moderate.py wp-update --limit "$WPUPDATE_LIMIT" || echo "(wp-update returned $?)"
  echo
  if [ "$FORMALIZE_LIMIT" -gt 0 ]; then
    echo "--- formalize backlog: verify vs live D1, then Agent-2 the extracted articles ---"
    # Run the reviewer ONLY if the verifier succeeded AND wrote a non-empty list
    # this run — never trust a possibly-stale file from a prior run (it would
    # burn tokens re-reviewing already-formalized articles).
    if python3 "$REPO/manage/formalize_backlog.py" --limit "$FORMALIZE_LIMIT" \
         && [ -s "$REPO/manage/data/formalize_slugs.txt" ]; then
      "$PY" moderate.py review --slugs "$REPO/manage/data/formalize_slugs.txt" \
            --limit "$FORMALIZE_LIMIT" --concurrency "$CONCURRENCY" \
            --budget-tokens "$FORMALIZE_BUDGET" || echo "(formalize review returned $?)"
    else
      echo "(no fresh verified backlog — skipping formalize review)"
    fi
    echo
  fi
  echo "--- review batch (search-verified) ---"
  "$PY" moderate.py review --limit "$REVIEW_LIMIT" --concurrency "$CONCURRENCY" \
        --budget-tokens "$BUDGET_TOKENS" || echo "(review returned $?)"
  echo
  if [ "${WIKILEAN_GRAPH_REFRESH:-1}" = "1" ]; then
    echo "--- refresh + deploy concept graph (verified @[wikidata] tags + live Mathlib coverage) ---"
    # Coverage now reflects tonight's formalization (moderate.py rewrites the disk
    # artifacts it posts), so recompute it, then rebuild the graph page + assets.
    python3 "$REPO/manage/coverage.py" || echo "(coverage refresh returned $?)"
    python3 "$REPO/site/build_graph_page.py" || echo "(graph build returned $?)"
    ( cd "$REPO/wiki" && node --experimental-strip-types scripts/build-public.ts ) \
      || echo "(build-public returned $?)"
    # Deploy ONLY when wiki/src is clean — an unattended run must never ship
    # uncommitted Worker WIP. Gated + fail-soft; wrangler uses the user's OAuth
    # session (accessible under launchd).
    if [ "${WIKILEAN_GRAPH_DEPLOY:-1}" = "1" ]; then
      if [ -z "$(cd "$REPO" && git status --porcelain wiki/src wiki/assets)" ]; then
        ( cd "$REPO/wiki" && npm run deploy ) || echo "(graph deploy returned $?)"
      else
        echo "(wiki/src dirty — skipping graph deploy; run 'cd wiki && npm run deploy' by hand)"
      fi
    fi
    echo
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/moderate-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
