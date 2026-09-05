#!/bin/bash
# WikiLean nightly moderation — invoked by launchd (org.wikilean.moderate).
#
# Runs as the logged-in user so the Claude Max-plan subscription login (read by
# the `claude` CLI the agent SDK spawns) is available. launchd hands us a bare
# environment, so the wrapper derives its checkout and validates host-local
# paths before doing any work.
#
# Sequence: flush any checkpointed-but-unposted work from a prior failed run
# (free), drift-sweep (wp-update, zero agent tokens), then a bounded review
# batch (search-verified Agent 2). Limits/budget are env-overridable so the
# same script serves the smoke test and the production schedule.
set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)" || exit 1
# shellcheck disable=SC1091
. "$SCRIPT_DIR/nightly-runtime.sh"
wikilean_ops_init || exit 1

# Force Max-subscription auth. launchd hands us a bare env (no key), but when
# this is launched interactively via run-now.sh it inherits ANTHROPIC_API_KEY
# from the user's profile — the SDK would then bill that (out-of-credits) API
# account and every agent call dies "Credit balance is too low" with 0 tokens.
# Scrub it so both launch paths use the Max login.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

if [ "${WIKILEAN_OPS_PREFLIGHT_ONLY:-0}" = "1" ]; then
  wikilean_ops_print_check
  exit 0
fi

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
# Shared implementation (reset-time-aware sleep, budget-stop detection,
# tunables): site/ops/retry-lib.sh.
. "$SCRIPT_DIR/retry-lib.sh"

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
  "$PY" "$REPO/manage/refresh.py" $MANAGE_PULL || echo "(manage refresh returned $?)"
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
    if "$PY" "$REPO/manage/formalize_backlog.py" --limit "$FORMALIZE_LIMIT" \
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
  if [ "${WIKILEAN_AUTO_DECIDE:-1}" = "1" ]; then
    echo "--- auto-decide proposals (deterministic, no LLM; Human-at-boundaries) ---"
    # site/resolve_proposals.py: R1 no-delta -> reject not_better; R2 verified
    # decl rename -> approve; R3 proof_wanted stub + 'partial' -> approve;
    # everything else stays pending. Fail-soft: a bad night here must never
    # kill the graph refresh below.
    "$PY" "$REPO/site/resolve_proposals.py" --submit \
      || echo "(auto-decide returned $? — proposals stay pending, night continues)"
    echo
  fi
  if [ "${WIKILEAN_GRAPH_REFRESH:-1}" = "1" ]; then
    echo "--- refresh crossrefs + frontier + coverage (no deploy) ---"
    # Coverage reflects tonight's formalization (moderate.py rewrites the disk
    # artifacts it posts). Crossref backfill first (fail-soft: atomic write
    # keeps the last good file) — the brain nightly consumes it at 02:20
    # tomorrow. The old graph/atlas KV refresh lived here until 2026-07-10.
    "$PY" "$REPO/catalog/mathlib_deps/fetch_crossrefs.py" || echo "(crossrefs fetch returned $? — using last good file)"
    # FormalConjectures frontier ingest — same fail-soft contract; the DRIFT
    # lines in its output are the frontier moving (open→solved flips).
    "$PY" "$REPO/catalog/ingest_formal_conjectures.py" || echo "(fc ingest returned $? — using last good file)"
    # The old concept-graph/atlas page + endpoint stack is DELETED (retired
    # 2026-07-10, tombstones destroyed 2026-08-04): /graph_data.json,
    # /atlas_data.json and /api/atlas are plain 404s, their builders are
    # deleted, and the brain nightly (brain-nightly.sh) owns the graph now.
    # Coverage still refreshes here — manage/ worklists consume it.
    "$PY" "$REPO/manage/coverage.py" || echo "(coverage returned $?)"
    echo
  fi
  # Community edges are reduced from one independently acquired and sealed D1
  # snapshot bundle. This job never performs live D1 acquisition implicitly.
  # Invalid/missing input skips the reducer and leaves the prior output intact.
  if [ "${WIKILEAN_COMMUNITY_HARVEST:-0}" = "1" ]; then
    echo "--- graduate community brain edges (sealed D1 bundle → community_edges.jsonl) ---"
    COMMUNITY_BUNDLE="${WIKILEAN_D1_SNAPSHOT_BUNDLE:-}"
    if [ -z "$COMMUNITY_BUNDLE" ] \
        || [ "${COMMUNITY_BUNDLE#/}" = "$COMMUNITY_BUNDLE" ] \
        || [ ! -d "$COMMUNITY_BUNDLE" ]; then
      echo "!!! WIKILEAN_COMMUNITY_HARVEST=1 requires an absolute existing WIKILEAN_D1_SNAPSHOT_BUNDLE directory"
      echo "!!! community harvest skipped; keeping the prior community_edges.jsonl"
    else
      COMMUNITY_BUNDLE="$(CDPATH= cd -- "$COMMUNITY_BUNDLE" && pwd -P)"
      "$PY" "$REPO/brain/harvest_community_edges.py" \
        --snapshot-bundle "$COMMUNITY_BUNDLE" \
        || echo "(community harvest returned $? — keeping the prior community_edges.jsonl)"
    fi
    echo
  fi
  # /decl/:name reverse citations — same success-gated KV pattern, but under
  # its OWN gate: pausing the graph refresh must not silently stop the /decl
  # cited_by refresh (they share nothing but the pattern).
  if [ "${WIKILEAN_DECLCITES_REFRESH:-1}" = "1" ]; then
    echo "--- refresh /decl reverse citations (KV declcites:v1) ---"
    if "$PY" "$REPO/site/build_decl_citations.py"; then
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
    if "$PY" "$REPO/site/build_library_decls.py"; then
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
  # Golden-fixture freshness: the engine.golden vitest gate compares the TS
  # engine against render.py's site/out pages; every moderation edit drifts
  # them and the gate rots into a permanent 171/733-style failure (it masked
  # regressions for weeks until the 2026-08-04 full regen). Re-render the whole
  # corpus nightly — local files only, ~0.1s/article, fail-soft per slug.
  if [ "${WIKILEAN_GOLDEN_REFRESH:-1}" = "1" ]; then
    echo "--- refresh golden fixtures (site/out, local only) ---"
    n_ok=0; n_fail=0
    for f in "$REPO"/site/out/*.html; do
      s="$(basename "$f" .html)"
      case "$s" in index|concepts|about|404|brain) continue;; esac
      [ -f "$REPO/site/annotations/$s.json" ] || continue
      [ -f "$REPO/site/cache/$s.html" ] || continue
      if "$PY" "$REPO/site/render.py" "$s" >/dev/null 2>&1; then
        n_ok=$((n_ok + 1))
      else
        n_fail=$((n_fail + 1))
      fi
    done
    echo "(golden fixtures: $n_ok re-rendered, $n_fail failed)"
    echo
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/moderate-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
