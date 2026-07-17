#!/bin/bash
# WikiLean nightly BRAIN refresh — invoked by launchd (org.wikilean.brain) at
# 02:20 local, deliberately BEFORE the 03:10 newtags and 03:20 moderation jobs
# so the annotation agents see tonight's graph and the three jobs don't fight
# over the Max window at once.
#
# Sequence (docs/BRAIN-V2.md "Nightly brain sync"; every step individually
# gated + fail-soft — a failed step logs loudly and the run continues where
# safe, and aborts the PUBLISH where not):
#   1. INGEST external DBs per cadence (daily / weekly / monthly stamps);
#      adapters are atomic-write + fail-soft, so a failed fetch keeps the
#      previous *_pages.jsonl intact.
#   2. AGENTS (WIKILEAN_BRAIN_AGENTS=1, off by default): brain/sync_agents.py
#      writes brain/proposals/*.jsonl ONLY — never brain/data.
#   3. FOLD + BUILD: fold_proposals -> build_nodes -> build_edges ->
#      test_acceptance (RED = abort publish, keep old shards) -> build_shards ->
#      build_cells -> test_cells -> build_cell_shards -> test_cell_shards
#      (the v3 atom layer; any RED aborts the publish the same way) ->
#      build_brain_page (the /brain page itself, so the page we PUBLISH is the
#      page we just BUILT, from the source the deploy gate verifies below).
#      Rollups are pinned — not rebuilt nightly.
#   4. PUBLISH (WIKILEAN_BRAIN_DEPLOY=1): build-public, then the GATED deploy —
#      `npm run deploy` ONLY if site/out/brain.html actually exists (build-public
#      skips a missing page silently, shipping the previous one), the checked-out
#      branch is `main` (no detached HEAD, no rebase/merge in progress), `git
#      status --porcelain -- wiki/ site/assets site/build_brain_page.py` is empty,
#      AND `npx tsc --noEmit` passes. Never ships uncommitted Worker WIP or
#      asset-source WIP. (The generated artifacts are gitignored — site/out/ at
#      .gitignore:48, site/assets/brain/ at :66 — so the clean-tree gate is a
#      statement about SOURCE; step 3 is what makes it a statement about the
#      shipped bytes too.)
#
# Runs as the logged-in user so the Claude Max-plan login is available to the
# agent step. launchd hands a bare environment: absolute paths, explicit PATH.
set -uo pipefail

REPO="/Users/jack/Desktop/LEAN/WikiLean"
PY="$REPO/catalog/.venv/bin/python3"   # venv with claude-agent-sdk (agent step)
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Force Max-subscription auth (see nightly-moderate.sh for the full rationale):
# an inherited ANTHROPIC_API_KEY would bill an out-of-credits API account and
# every agent call dies with 0 tokens. Scrub it so all launch paths use Max.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

# Editable tunables live in site/ops/nightly.env (sourced with ":=" so a
# one-off env override still wins). Missing file → the inline defaults below.
[ -f "$REPO/site/ops/nightly.env" ] && . "$REPO/site/ops/nightly.env"

BRAIN_REFRESH="${WIKILEAN_BRAIN_REFRESH:-1}"
BRAIN_AGENTS="${WIKILEAN_BRAIN_AGENTS:-0}"
BRAIN_AGENT_BUDGET="${WIKILEAN_BRAIN_AGENT_BUDGET:-500000}"
BRAIN_DEPLOY="${WIKILEAN_BRAIN_DEPLOY:-0}"

LOGDIR="$REPO/site/ops/logs"
mkdir -p "$LOGDIR"
TS="$(date +%Y%m%dT%H%M%S)"
LOG="$LOGDIR/brain-$TS.log"

if [ "$BRAIN_REFRESH" != "1" ]; then
  echo "[$TS] brain refresh disabled (WIKILEAN_BRAIN_REFRESH=$BRAIN_REFRESH) — skipping" >>"$LOGDIR/skips.log"
  exit 0
fi

# Retry the agent step across a Max-window reset — same contract as the
# moderation wrapper: exit 3 + the rate-limit signature in the log tail means
# window exhaustion (retry); exit 3 without it is an intended budget stop.
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

# Single-instance lock — its OWN lock (.lock.brain.d) so this job coexists with
# the 03:10/03:20 jobs. Atomic mkdir, 4h stale recovery.
LOCKDIR="$LOGDIR/.lock.brain.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    rmdir "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null || { echo "[$TS] brain lock race — skipping" >>"$LOGDIR/skips.log"; exit 0; }
  else
    echo "[$TS] previous brain run still active — skipping" >>"$LOGDIR/skips.log"
    exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

# Run an ingest/build python script fail-soft: a missing script (adapter not
# landed yet) or a nonzero exit logs and CONTINUES — every adapter is
# atomic-write, so the previous data stays intact either way.
py_soft() {
  local label="$1" script="$2"; shift 2
  echo "--- $label ---"
  if [ ! -f "$script" ]; then
    echo "($script missing — skipped)"
    return 0
  fi
  python3 "$script" "$@" || echo "($label returned $? — previous data intact, continuing)"
}

# Cadence stamps: due <name> <days> is true when .stamp.<name> is missing or
# older than <days> days. Stamps are touched after the ATTEMPT (adapters keep
# their own caching/staleness, so a flaky source doesn't re-trigger the whole
# weekly block every night).
due() {
  local s="$LOGDIR/.stamp.$1"
  [ ! -f "$s" ] && return 0
  [ -n "$(find "$s" -maxdepth 0 -mtime +"$2" 2>/dev/null)" ]
}

cd "$REPO" || exit 1
{
  echo "=== WikiLean nightly BRAIN refresh $TS ==="
  echo "agents=$BRAIN_AGENTS budget=$BRAIN_AGENT_BUDGET deploy=$BRAIN_DEPLOY"
  echo

  # ---- 1. INGEST (per-source cadence; each adapter fail-soft) ----------------
  echo "=== ingest: daily sources ==="
  py_soft "nlab ingest"        "$REPO/brain/ingest/nlab.py"
  py_soft "proofwiki ingest"   "$REPO/brain/ingest/proofwiki.py"
  py_soft "oeis ingest"        "$REPO/brain/ingest/oeis.py"
  py_soft "stacks ingest"      "$REPO/brain/ingest/stacks.py"
  py_soft "mathlib @[wikidata]/@[stacks]/@[kerodon] tag harvest" \
                               "$REPO/catalog/harvest_mathlib_tags.py"
  py_soft "wikidata crossrefs fetch" \
                               "$REPO/catalog/mathlib_deps/fetch_crossrefs.py"
  echo
  if due brain-weekly 6; then
    echo "=== ingest: weekly sources ==="
    py_soft "lmfdb ingest (Postgres mirror)" "$REPO/brain/ingest/lmfdb.py"
    py_soft "eom ingest"                     "$REPO/brain/ingest/eom.py"
    py_soft "planetmath ingest"              "$REPO/brain/ingest/planetmath.py"
    py_soft "wikidata descriptions"          "$REPO/brain/ingest/wikidata_descriptions.py"
    py_soft "formal-conjectures harvest"     "$REPO/brain/ingest/formal_conjectures.py"
    py_soft "erdosproblems ingest"           "$REPO/brain/ingest/erdosproblems.py"
    touch "$LOGDIR/.stamp.brain-weekly"
  else
    echo "(weekly sources not due — skipping lmfdb/eom/planetmath/descriptions/formal-conjectures/erdos)"
  fi
  echo
  if due brain-monthly 27; then
    echo "=== ingest: monthly sources ==="
    py_soft "kerodon ingest"   "$REPO/brain/ingest/kerodon.py"
    py_soft "dlmf ingest"      "$REPO/brain/ingest/dlmf.py"
    py_soft "mathworld ingest" "$REPO/brain/ingest/mathworld.py"
    py_soft "openalex citations" "$REPO/brain/ingest/openalex_citations.py"
    touch "$LOGDIR/.stamp.brain-monthly"
  else
    echo "(monthly sources not due — skipping kerodon/dlmf/mathworld)"
  fi
  echo

  # ---- 2. AGENTS (propose-only; off until Jack enables) ----------------------
  if [ "$BRAIN_AGENTS" = "1" ]; then
    echo "=== agent team: cartographer + skeptic (writes brain/proposals/ only) ==="
    retry_on_ratelimit "$PY" "$REPO/brain/sync_agents.py" \
        --budget-tokens "$BRAIN_AGENT_BUDGET" \
      || echo "(sync_agents returned $? — proposals may be partial; the fold gates everything)"
  else
    echo "(agent team disabled — WIKILEAN_BRAIN_AGENTS=0)"
  fi
  echo

  # ---- 3. FOLD + BUILD (abort publish on failure, keep old shards) -----------
  echo "=== fold proposals (deterministic verifier; network: Wikidata) ==="
  python3 "$REPO/brain/fold_proposals.py" \
    || echo "(fold returned $? — building from the last folded outputs)"
  echo
  PUBLISH_OK=1
  echo "=== rebuild brain graph (rollups are pinned — not rebuilt nightly) ==="
  if ! python3 "$REPO/brain/build_nodes.py"; then
    echo "!!! build_nodes FAILED — publish aborted (old nodes.jsonl intact)"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ] && ! python3 "$REPO/brain/build_edges.py"; then
    echo "!!! build_edges FAILED — publish aborted (old edges.jsonl intact)"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if python3 "$REPO/brain/test_acceptance.py"; then
      echo "(acceptance GREEN)"
    else
      echo "!!! test_acceptance RED — publish aborted, old shards stay live"
      PUBLISH_OK=0
    fi
  fi
  if [ "$PUBLISH_OK" = "1" ] && ! python3 "$REPO/brain/build_shards.py"; then
    echo "!!! build_shards FAILED — publish aborted, old shards stay live"
    PUBLISH_OK=0
  fi

  # ---- the v3 ATOM layer (brain/SCHEMA.md#v3) --------------------------------
  # Organs -> cells -> supercells -> synapses, then the cell shards the client
  # reads. Same discipline as above: acceptance RED aborts the publish and the old
  # shards stay live. build_cells runs the force layout (~3min), which is why the
  # client no longer simulates anything.
  if [ "$PUBLISH_OK" = "1" ] && ! python3 "$REPO/brain/build_cells.py"; then
    echo "!!! build_cells FAILED — publish aborted (old cells.jsonl intact)"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if python3 "$REPO/brain/test_cells.py"; then
      echo "(cell acceptance GREEN)"
    else
      echo "!!! test_cells RED — publish aborted, old cell shards stay live"
      PUBLISH_OK=0
    fi
  fi
  if [ "$PUBLISH_OK" = "1" ] && ! python3 "$REPO/brain/build_cell_shards.py"; then
    echo "!!! build_cell_shards FAILED — publish aborted, old cell shards stay live"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if python3 "$REPO/brain/test_cell_shards.py"; then
      echo "(cell shard acceptance GREEN)"
    else
      echo "!!! test_cell_shards RED — publish aborted, old cell shards stay live"
      PUBLISH_OK=0
    fi
  fi
  # ---- the /brain PAGE (site/build_brain_page.py -> site/out/brain.html) ------
  # This step was MISSING, and the page is what a reader actually looks at. The
  # nightly rebuilt the shards and then shipped whatever brain.html happened to be
  # sitting on disk at 02:20: site/out/ is gitignored (.gitignore:48), so the file
  # survives `git checkout` and can be an artifact of a completely different branch
  # than the one the deploy gate just verified as clean and on main. The page and the
  # shards are version-coupled — the v3 page reads /assets/brain/cells/ — so a stale
  # page against fresh shards is exactly how /brain dies on "data unavailable".
  # Built here, under the same PUBLISH_OK gate as everything else: if the shards
  # failed acceptance we do NOT refresh the page either, so the old page and the old
  # shards stay live TOGETHER (consistent), which is the whole point of the gate.
  if [ "$PUBLISH_OK" = "1" ]; then
    if python3 "$REPO/site/build_brain_page.py" && [ -s "$REPO/site/out/brain.html" ]; then
      echo "(brain page rebuilt: $(wc -c <"$REPO/site/out/brain.html" | tr -d ' ') B)"
    else
      echo "!!! build_brain_page FAILED (or wrote no page) — publish aborted, the live page stays"
      PUBLISH_OK=0
    fi
  fi

  # The tagger-quality worklist: cells that ballooned via a bad AI grade. Not a
  # gate — a signal (SCHEMA "A ballooning cell is a TAGGER signal").
  if [ "$PUBLISH_OK" = "1" ] && [ -f "$REPO/brain/data/cell_review.jsonl" ]; then
    n_flagged=$(( $(wc -l < "$REPO/brain/data/cell_review.jsonl") - 1 ))
    echo "(cell_review: $n_flagged cells flagged for tagger re-grading)"
  fi
  echo

  # ---- 4. PUBLISH (clean-tree-gated deploy) -----------------------------------
  if [ "$PUBLISH_OK" = "1" ] && [ "$BRAIN_DEPLOY" = "1" ]; then
    echo "=== publish: build-public + clean-tree-gated deploy ==="
    # Guard the ARTIFACT, not just its source. build-public copies site/out/brain.html
    # only `if (existsSync(src))` — a missing page is silently skipped and
    # wiki/public/ keeps the PREVIOUS brain.html, so a stale page ships against
    # tonight's shards with no error anywhere. The build step above is gated on this
    # same file, so reaching here with it missing means something deleted it since.
    if [ ! -s "$REPO/site/out/brain.html" ]; then
      echo "!!! SKIPPED-DEPLOY: site/out/brain.html missing/empty — build-public would"
      echo "    silently ship the previous page against tonight's shards"
    elif ! (cd "$REPO/wiki" && node --experimental-strip-types scripts/build-public.ts); then
      echo "!!! SKIPPED-DEPLOY: build-public failed — shards rebuilt on disk but NOT shipped"
    else
      # Deploy gate: main-branch only, no rebase/merge in flight, and a clean
      # tree across everything the deploy bakes in — wiki/ (npm run deploy
      # bundles ALL of wiki/src) plus the build-public asset sources.
      BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
      GITDIR="$(git -C "$REPO" rev-parse --git-dir 2>/dev/null)"
      case "$GITDIR" in /*) ;; *) GITDIR="$REPO/$GITDIR" ;; esac
      DIRTY="$(git -C "$REPO" status --porcelain -- wiki/ site/assets site/build_brain_page.py)"
      if [ "$BRANCH" != "main" ]; then
        echo "!!! SKIPPED-DEPLOY: checked-out branch is '${BRANCH:-unknown}', not 'main' (detached HEAD reports 'HEAD') — not deploying"
      elif [ -d "$GITDIR/rebase-merge" ] || [ -d "$GITDIR/rebase-apply" ] || [ -f "$GITDIR/MERGE_HEAD" ]; then
        echo "!!! SKIPPED-DEPLOY: rebase/merge in progress ($GITDIR) — not deploying"
      elif [ -n "$DIRTY" ]; then
        echo "!!! SKIPPED-DEPLOY: uncommitted wiki//site-asset changes would ship — commit or stash first:"
        echo "$DIRTY"
      elif ! (cd "$REPO/wiki" && npx tsc --noEmit); then
        echo "!!! SKIPPED-DEPLOY: npx tsc --noEmit failed — fix wiki/src before the nightly can deploy"
      elif (cd "$REPO/wiki" && npm run deploy); then
        echo "(deployed — rebuilt brain shards are live)"
      else
        echo "!!! DEPLOY FAILED (npm run deploy) — production keeps the previous shards"
      fi
    fi
  elif [ "$PUBLISH_OK" = "1" ]; then
    echo "(deploy disabled — WIKILEAN_BRAIN_DEPLOY=0; rebuilt shards stay local until the next manual deploy)"
  else
    echo "!!! PUBLISH ABORTED — see the build/acceptance failure above; live shards unchanged"
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/brain-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
