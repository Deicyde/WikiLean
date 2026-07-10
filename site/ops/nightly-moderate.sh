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

# Retry an agent step across a Max-window reset. moderate.py's run loop exits 3
# on a consecutive-window-exhaustion abort AND prints "hit your limit"; that same
# exit 3 is ALSO used for an intentional token-budget stop, so we retry ONLY when
# the fresh log tail carries the Max rate-limit signature — never on a budget
# stop. Bounded (default 3 tries × 15 min) so a stuck night can't run into the
# morning. Rationale: launchd fires at a fixed clock time but the Max 5-hour
# window resets on a rolling schedule, so any fixed start can still straddle a
# reset (the 2026-07-02 run lost all 29 jobs to a 03:10 reset). See nightly.env.
RETRY_SLEEP="${WIKILEAN_RETRY_SLEEP:-900}"   # seconds to wait for the window reset
RETRY_MAX="${WIKILEAN_RETRY_MAX:-3}"
retry_on_ratelimit() {
  local n=0 rc
  while : ; do
    "$@"; rc=$?
    [ "$rc" -ne 3 ] && return "$rc"
    if ! tail -n 80 "$LOG" 2>/dev/null | grep -qiE "hit your limit|usage limit|resets [0-9]"; then
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
  if [ "${WIKILEAN_WD_EMBED_REFRESH:-1}" = "1" ]; then
    echo "--- refresh Wikidata semantic index (rebuild only if universe is newer) ---"
    # Powers the wikidata_semantic tool (Agent 2 meaning-based retrieval). Rebuild
    # only when the curated universe changed, so the nightly cost is normally zero.
    # Fail-soft: a build failure keeps the last good .npz (query still works).
    WD_UNIVERSE="$REPO/catalog/data/wikidata_universe.jsonl"
    WD_NPZ="$REPO/catalog/data/wikidata_embeddings.npz"
    if [ ! -f "$WD_NPZ" ] || [ "$WD_UNIVERSE" -nt "$WD_NPZ" ]; then
      "$PY" "$REPO/catalog/build_wikidata_embeddings.py" \
        || echo "(wikidata embeddings rebuild returned $? — keeping last good .npz)"
    else
      echo "(wikidata embeddings up to date — skipping)"
    fi
    echo
  fi
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
      retry_on_ratelimit "$PY" moderate.py review --slugs "$REPO/manage/data/formalize_slugs.txt" \
            --limit "$FORMALIZE_LIMIT" --concurrency "$CONCURRENCY" \
            --budget-tokens "$FORMALIZE_BUDGET" || echo "(formalize review returned $?)"
    else
      echo "(no fresh verified backlog — skipping formalize review)"
    fi
    echo
  fi
  echo "--- review batch (search-verified) ---"
  retry_on_ratelimit "$PY" moderate.py review --limit "$REVIEW_LIMIT" --concurrency "$CONCURRENCY" \
        --budget-tokens "$BUDGET_TOKENS" || echo "(review returned $?)"
  echo
  if [ "${WIKILEAN_GRAPH_REFRESH:-1}" = "1" ]; then
    echo "--- refresh crossrefs + frontier + coverage (no deploy) ---"
    # Coverage reflects tonight's formalization (moderate.py rewrites the disk
    # artifacts it posts). Crossref backfill first (fail-soft: atomic write
    # keeps the last good file) — the brain nightly consumes it at 02:20
    # tomorrow. The old graph/atlas KV refresh lived here until 2026-07-10.
    python3 "$REPO/catalog/mathlib_deps/fetch_crossrefs.py" || echo "(crossrefs fetch returned $? — using last good file)"
    # FormalConjectures frontier ingest — same fail-soft contract; the DRIFT
    # lines in its output are the frontier moving (open→solved flips).
    python3 "$REPO/catalog/ingest_formal_conjectures.py" || echo "(fc ingest returned $? — using last good file)"
    # The old concept-graph/atlas page + endpoint stack is RETIRED (2026-07-10):
    # /graph_data.json, /atlas_data.json and /api/atlas answer 410, their
    # builders are deleted, and the brain nightly (brain-nightly.sh) owns the
    # graph now. Coverage still refreshes here — manage/ worklists consume it.
    python3 "$REPO/manage/coverage.py" || echo "(coverage returned $?)"
    echo
  fi
  # Community brain edges (docs/BRAIN-EDITS-ROADMAP.md phase 4): snapshot the live
  # D1 tail into brain/data/community_edges.jsonl (human edges trusted; AI edges
  # verified against the oracle). This is the graduation record the brain rebuild
  # folds into the static base. Fail-soft — the graph refresh already ran. The
  # brain SHARD rebuild + deploy is a separate step (not yet in this nightly).
  if [ "${WIKILEAN_COMMUNITY_HARVEST:-1}" = "1" ]; then
    echo "--- graduate community brain edges (D1 → community_edges.jsonl) ---"
    python3 "$REPO/brain/harvest_community_edges.py" \
      || echo "(community harvest returned $? — keeping the last snapshot)"
    echo
  fi
  # /decl/:name reverse citations — same success-gated KV pattern, but under
  # its OWN gate: pausing the graph refresh must not silently stop the /decl
  # cited_by refresh (they share nothing but the pattern).
  if [ "${WIKILEAN_DECLCITES_REFRESH:-1}" = "1" ]; then
    echo "--- refresh /decl reverse citations (KV declcites:v1) ---"
    if python3 "$REPO/site/build_decl_citations.py"; then
      if [ "${WIKILEAN_GRAPH_DEPLOY:-1}" = "1" ]; then
        ( cd "$REPO/wiki" && npx wrangler kv key put --binding=RENDER_CACHE --remote \
            declcites:v1 --path="$REPO/site/out/decl_citations.json" ) \
          || echo "(declcites kv put returned $?)"
      fi
    else
      echo "(decl-citations build failed — keeping the last KV copy)"
    fi
    echo
  fi
  # Multi-library decl fabric (CSLib / Physlib / Formal Conjectures own-decl
  # indexes → KV libdecls:v1). Same success-gated pattern; per-library
  # fail-soft lives inside the builder (a dead docs site keeps its last blob).
  if [ "${WIKILEAN_LIBDECLS_REFRESH:-1}" = "1" ]; then
    echo "--- refresh multi-library decl fabric (KV libdecls:v1) ---"
    if python3 "$REPO/site/build_library_decls.py"; then
      if [ "${WIKILEAN_GRAPH_DEPLOY:-1}" = "1" ]; then
        ( cd "$REPO/wiki" && npx wrangler kv key put --binding=RENDER_CACHE --remote \
            libdecls:v1 --path="$REPO/site/out/library_decls.json" ) \
          || echo "(libdecls kv put returned $?)"
      fi
    else
      echo "(library-decls build failed — keeping the last KV copy)"
    fi
    echo
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/moderate-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
