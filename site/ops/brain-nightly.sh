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
#   3. FOLD + BUILD: fold_proposals -> build_snapshot (nodes + both edge
#      streams + the local SQLite index in one generation) -> test_acceptance
#      (RED = abort publish, keep old shards) -> build_shards ->
#      build_cells -> test_cells -> build_frontier (the homeless-cell partition,
#      brain/data/frontier.jsonl) -> build_cell_shards -> test_cell_shards ->
#      test_frontier (the v3 atom layer; any RED aborts the publish the same way) ->
#      build_brain_page (the /brain page itself, so the page we PUBLISH is the
#      page we just BUILT, from the source the deploy gate verifies below).
#      Rollups are pinned — not rebuilt nightly.
#   4. RELEASE: freeze one content-addressed release, independently verify it,
#      stage its current/previous immutable public namespaces plus compatibility
#      aliases, then typecheck and test the Worker against those exact bytes.
#   5. DEPLOY (WIKILEAN_BRAIN_DEPLOY=1, off by default): record the current
#      production selector and unambiguous 100% Worker version, deploy exactly
#      once, then canary selector/manifest/shard/page/REST/MCP/cursor/aliases. A failed
#      canary never overwrites a newer deployment: automatic rollback is OFF by
#      default and, when explicitly enabled in an exclusive window, is guarded
#      by repeated Worker-version and selector checks.
#
# Runs as the logged-in user so the Claude Max-plan login is available to the
# agent step. launchd hands a bare environment: absolute paths, explicit PATH.
set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)" || exit 1
REPO="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Refuse to run from a copied/misplaced script: all release paths below are
# derived from this physical root, so these anchors establish that it is WikiLean.
for required in \
  "$REPO/brain/authority/reducer-inputs-v1.json" \
  "$REPO/site/build_brain_page.py" \
  "$REPO/wiki/package.json" \
  "$REPO/wiki/wrangler.jsonc"; do
  if [ ! -f "$required" ]; then
    printf 'brain-nightly: derived repository root %s is invalid (missing %s)\n' "$REPO" "$required" >&2
    exit 1
  fi
done

# Force Max-subscription auth (see nightly-moderate.sh for the full rationale):
# an inherited ANTHROPIC_API_KEY would bill an out-of-credits API account and
# every agent call dies with 0 tokens. Scrub it so all launch paths use Max.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

# Editable tunables live in site/ops/nightly.env (sourced with ":=" so a
# one-off env override still wins). Host-local absolute paths belong in the
# gitignored nightly.local.env, sourced afterward. Missing files use defaults.
[ -f "$REPO/site/ops/nightly.env" ] && . "$REPO/site/ops/nightly.env"
[ -f "$REPO/site/ops/nightly.local.env" ] && . "$REPO/site/ops/nightly.local.env"

# launchd's system Python is 3.9 on this host, below WikiLean's supported 3.12.
# Select an explicit interpreter and fail before ingest/build work if it is not
# suitable. The optional agent SDK keeps its separate virtualenv interpreter.
if [ -n "${WIKILEAN_PYTHON:-}" ]; then
  PYTHON_BIN="$WIKILEAN_PYTHON"
else
  PYTHON_BIN=""
  for candidate in "$REPO/.venv/bin/python3" /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if [ -x "$candidate" ]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  [ -n "$PYTHON_BIN" ] || PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
fi
AGENT_PY="${WIKILEAN_BRAIN_AGENT_PYTHON:-$REPO/catalog/.venv/bin/python3}"

BRAIN_REFRESH="${WIKILEAN_BRAIN_REFRESH:-1}"
BRAIN_AGENTS="${WIKILEAN_BRAIN_AGENTS:-0}"
BRAIN_AGENT_BUDGET="${WIKILEAN_BRAIN_AGENT_BUDGET:-500000}"
BRAIN_DEPLOY="${WIKILEAN_BRAIN_DEPLOY:-0}"
BRAIN_AUTO_ROLLBACK="${WIKILEAN_BRAIN_AUTO_ROLLBACK:-0}"
BRAIN_MATHLIB_CHECKOUT="${BRAIN_MATHLIB_CHECKOUT:-}"
if [ -n "$BRAIN_MATHLIB_CHECKOUT" ]; then
  export BRAIN_MATHLIB_CHECKOUT
else
  export -n BRAIN_MATHLIB_CHECKOUT 2>/dev/null || true
fi
BRAIN_SEMANTIC_EPOCH="${WIKILEAN_BRAIN_SEMANTIC_EPOCH:-brain-v3-current}"
BRAIN_REDUCER_SCHEDULE="${WIKILEAN_BRAIN_REDUCER_SCHEDULE:-brain-v3-current}"
BRAIN_REDUCER_VERSION="${WIKILEAN_BRAIN_REDUCER_VERSION:-1}"
BRAIN_CANARY_URL="${WIKILEAN_BRAIN_CANARY_URL:-https://wikilean.jackmccarthy.org}"
BRAIN_CANARY_TIMEOUT="${WIKILEAN_BRAIN_CANARY_TIMEOUT:-300}"
BRAIN_CANARY_INTERVAL="${WIKILEAN_BRAIN_CANARY_INTERVAL:-5}"
BRAIN_CANARY_MAX_RESPONSE_BYTES="${WIKILEAN_BRAIN_CANARY_MAX_RESPONSE_BYTES:-33554432}"
BRAIN_STATUS_ATTEMPTS="${WIKILEAN_BRAIN_STATUS_ATTEMPTS:-12}"
BRAIN_STATUS_INTERVAL="${WIKILEAN_BRAIN_STATUS_INTERVAL:-5}"
RELEASE_STORE="$REPO/site/out/brain-releases"
RELEASE_RESULT="$REPO/site/out/brain-release-result.json"
RELEASE_METRICS_RESULT="$REPO/site/out/brain-release-metrics.json"
PUBLIC_RESULT="$REPO/site/out/brain-public-result.json"
CANARY_RESULT="$REPO/site/out/brain-canary-result.json"

LOGDIR="$REPO/site/ops/logs"
mkdir -p "$LOGDIR"
TS="$(date +%Y%m%dT%H%M%S)"
LOG="$LOGDIR/brain-$TS.log"
RUN_STATUS=0

if [ "$BRAIN_REFRESH" != "1" ]; then
  echo "[$TS] brain refresh disabled (WIKILEAN_BRAIN_REFRESH=$BRAIN_REFRESH) — skipping" >>"$LOGDIR/skips.log"
  exit 0
fi

# Retry the agent step across a Max-window reset — same contract as the
# moderation wrapper; shared implementation (reset-time-aware sleep,
# budget-stop detection): site/ops/retry-lib.sh.
. "$(dirname "$0")/retry-lib.sh"

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

# A new enabled run invalidates every prior machine-readable result immediately,
# even if runtime/input preflight fails or the process is interrupted later.
mkdir -p "$REPO/site/out"
rm -f "$RELEASE_RESULT" "$RELEASE_METRICS_RESULT" "$PUBLIC_RESULT" "$CANARY_RESULT"
if [ ! -x "$PYTHON_BIN" ] \
    || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  {
    echo "=== WikiLean nightly BRAIN refresh $TS ==="
    echo "!!! Python 3.12+ is required (selected ${PYTHON_BIN:-none})"
  } >>"$LOG" 2>&1
  exit 1
fi
case "$BRAIN_STATUS_ATTEMPTS" in
  ''|*[!0-9]*|0)
    echo "!!! WIKILEAN_BRAIN_STATUS_ATTEMPTS must be a positive integer" >>"$LOG"
    exit 1
    ;;
esac
case "$BRAIN_STATUS_INTERVAL" in
  ''|*[!0-9]*)
    echo "!!! WIKILEAN_BRAIN_STATUS_INTERVAL must be a non-negative integer" >>"$LOG"
    exit 1
    ;;
esac
if [ -z "$BRAIN_MATHLIB_CHECKOUT" ] || [ ! -d "$BRAIN_MATHLIB_CHECKOUT/Algebra" ]; then
  {
    echo "=== WikiLean nightly BRAIN refresh $TS ==="
    echo "!!! BRAIN_MATHLIB_CHECKOUT must name a readable mathlib4/Mathlib directory"
    echo "    configured value: ${BRAIN_MATHLIB_CHECKOUT:-unset}"
  } >>"$LOG" 2>&1
  exit 1
fi

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
  "$PYTHON_BIN" "$script" "$@" || echo "($label returned $? — previous data intact, continuing)"
}

# Extract the last JSON object containing all requested keys from noisy command
# output. Wrangler and npm may add warnings; release gates must not depend on a
# particular line being last or on human-oriented formatting.
extract_json_object() {
  local input="$1" output="$2"; shift 2
  "$PYTHON_BIN" - "$input" "$output" "$@" <<'PY'
import json
import sys
from pathlib import Path

source, destination, *required = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8")
decoder = json.JSONDecoder()
matches = []
for start, char in enumerate(text):
    if char != "{":
        continue
    try:
        value, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict) and all(key in value for key in required):
        matches.append(value)
if not matches:
    raise SystemExit(f"no JSON object containing {required!r} found in {source}")
Path(destination).write_text(json.dumps(matches[-1], sort_keys=True) + "\n", encoding="utf-8")
PY
}

json_field() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
field = value.get(sys.argv[2])
if not isinstance(field, str) or not field:
    raise SystemExit(f"missing string field {sys.argv[2]!r}")
print(field)
PY
}

public_result_release_id() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(value, dict):
    raise SystemExit("public build result must be an object")
brain = value.get("brain")
if value.get("schema") != "wikilean.public-build-result/v1" or not isinstance(brain, dict):
    raise SystemExit("invalid public build result schema")
if brain.get("schema") != "wikilean.public-stage-result/v1":
    raise SystemExit("invalid public stage result schema")
if brain.get("warnings") != []:
    raise SystemExit("public stage completed with operational warnings")
page = brain.get("brain_page")
if (
    not isinstance(page, dict)
    or not isinstance(page.get("destination"), str)
    or not page.get("destination")
    or not isinstance(page.get("bytes"), int)
    or isinstance(page.get("bytes"), bool)
    or page.get("bytes") < 0
    or not isinstance(page.get("sha256"), str)
):
    raise SystemExit("public stage did not record a verified frozen Brain page")
for field in (
    "objects", "bytes", "largest_file_bytes", "copy_buffer_bytes",
    "duration_ms", "max_rss_bytes", "free_bytes_before", "free_bytes_after",
):
    number = brain.get(field)
    if not isinstance(number, (int, float)) or isinstance(number, bool) or number < 0:
        raise SystemExit(f"invalid public stage metric {field!r}")
release_id = brain.get("release_id")
if not isinstance(release_id, str) or not release_id:
    raise SystemExit("public stage result has no release_id")
print(release_id)
PY
}

store_metrics_release_id() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
if not isinstance(value, dict) or value.get("schema") != "wikilean.brain.store-metrics.v1":
    raise SystemExit("invalid Brain store metrics schema")
if value.get("ok") is not True or value.get("warnings") != []:
    raise SystemExit("Brain store metrics reported a failed check or warning")
identity = value.get("identity")
database = value.get("database")
analyze = value.get("analyze")
if not isinstance(identity, dict) or not isinstance(database, dict) or not isinstance(analyze, dict):
    raise SystemExit("Brain store metrics omitted required sections")
user_version = database.get("user_version")
if (
    not isinstance(user_version, int)
    or isinstance(user_version, bool)
    or user_version < 2
    or analyze.get("present") is not True
):
    raise SystemExit("Brain store is not schema v2 with persisted planner statistics")
for field in ("duration_ms", "max_rss_bytes"):
    number = value.get(field)
    if not isinstance(number, (int, float)) or isinstance(number, bool) or number < 0:
        raise SystemExit(f"invalid Brain store metric {field!r}")
release_id = identity.get("release_id")
if not isinstance(release_id, str) or not release_id:
    raise SystemExit("Brain store metrics has no release identity")
print(release_id)
PY
}

sha256_text() {
  "$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

# Wrangler 4.120.0 `deployments status --json` reports a `versions` array. Only
# one version at exactly 100% is safe for unattended rollback.
wrangler_sole_version() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
versions = value.get("versions") if isinstance(value, dict) else None
if not isinstance(versions, list) or len(versions) != 1:
    raise SystemExit(1)
row = versions[0]
if not isinstance(row, dict) or row.get("percentage") != 100:
    raise SystemExit(1)
version = row.get("version_id")
if not isinstance(version, str) or not version:
    raise SystemExit(1)
print(version)
PY
}

# Wrangler 4.120 emits one stable `Current Version ID: <uuid>` line after a
# successful deploy. Bind the canary to that candidate, never to a later status
# lookup that could already reflect an unrelated deployment.
wrangler_deployed_version() {
  "$PYTHON_BIN" - "$1" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
matches = re.findall(
    r"(?m)^\s*Current Version ID:\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$",
    text,
)
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
PY
}

selector_release_ids() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import json
import re
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
candidate = sys.argv[2]
allowed = {
    "schema", "release_id", "release", "manifest",
    "previous_release_id", "previous_release", "previous_manifest", "audited_at",
}
if not isinstance(value, dict) or set(value) - allowed:
    raise SystemExit(1)
def checked(release_id, release, manifest):
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", release_id or "")
    if not match or release != match.group(1):
        raise SystemExit(1)
    if manifest != f"/assets/brain/releases/{release}/release.json":
        raise SystemExit(1)
    return release_id
if value.get("schema") != "wikilean.release-selector/v1":
    raise SystemExit(1)
current = checked(value.get("release_id"), value.get("release"), value.get("manifest"))
previous_keys = ["previous_release_id", "previous_release", "previous_manifest"]
present_previous = [key for key in previous_keys if key in value]
has_previous = bool(present_previous)
if has_previous and len(present_previous) != len(previous_keys):
    raise SystemExit(1)
previous_values = [value.get(key) for key in previous_keys]
previous = checked(*previous_values) if has_previous else ""
if previous == current:
    raise SystemExit(1)
if value.get("audited_at") is not None and (
    not isinstance(value.get("audited_at"), str) or not value.get("audited_at")
):
    raise SystemExit(1)
retained = previous if current == candidate else current
print(current)
print(retained or "-")
PY
}

run_release_canary() {
  local expected_release_id="$1"
  local raw normalized actual_release_id
  raw="$(mktemp "$LOGDIR/.brain-canary.XXXXXX")"
  normalized="$(mktemp "$REPO/site/out/.brain-canary-result.XXXXXX")"
  if "$PYTHON_BIN" "$REPO/site/ops/brain-canary.py" \
      --base-url "$BRAIN_CANARY_URL" \
      --expected-release-id "$expected_release_id" \
      --timeout "$BRAIN_CANARY_TIMEOUT" \
      --interval "$BRAIN_CANARY_INTERVAL" \
      --max-response-bytes "$BRAIN_CANARY_MAX_RESPONSE_BYTES" >"$raw" \
    && extract_json_object "$raw" "$normalized" \
         schema ok release_id attempts convergence_seconds requests response_bytes \
    && actual_release_id="$(json_field "$normalized" release_id 2>/dev/null)" \
    && [ "$actual_release_id" = "$expected_release_id" ]; then
    mv "$normalized" "$CANARY_RESULT"
    cat "$CANARY_RESULT"
    rm -f "$raw"
    return 0
  fi
  [ -s "$raw" ] && cat "$raw"
  rm -f "$raw" "$normalized"
  return 1
}

wait_for_worker_version() {
  local expected_version="$1" output="$2"
  local attempt actual_version
  [ -n "$expected_version" ] || return 1
  for ((attempt = 1; attempt <= BRAIN_STATUS_ATTEMPTS; attempt++)); do
    if (cd "$REPO/wiki" \
        && npx --no-install wrangler deployments status --json >"$output"); then
      actual_version="$(wrangler_sole_version "$output" 2>/dev/null || true)"
      if [ "$actual_version" = "$expected_version" ]; then
        return 0
      fi
    fi
    [ "$attempt" -eq "$BRAIN_STATUS_ATTEMPTS" ] || sleep "$BRAIN_STATUS_INTERVAL"
  done
  return 1
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
    py_soft "tauceti harvest"                "$REPO/brain/ingest/lean_repo.py" tauceti
    # User-registered Lean repos: sync the enabled list from the live Worker
    # (pinned contract: GET /api/repos/enabled -> {"repos":[{owner,repo,lib}]}),
    # fail-soft — a failed curl keeps the previous registrations.json, and a
    # missing file makes the harvest a no-op. The per-repo clone/harvest loop
    # (caps: 50 repos, 20k decls each) lives in lean_repo.py --user-repos.
    mkdir -p "$REPO/catalog/data/user_repos"
    if curl -fsS --max-time 120 "https://wikilean.jackmccarthy.org/api/repos/enabled" \
        -o "$REPO/catalog/data/user_repos/registrations.json.tmp"; then
      mv "$REPO/catalog/data/user_repos/registrations.json.tmp" \
         "$REPO/catalog/data/user_repos/registrations.json"
      echo "(user-repo registrations synced)"
    else
      rm -f "$REPO/catalog/data/user_repos/registrations.json.tmp"
      echo "(registrations sync failed — keeping the previous file)"
    fi
    py_soft "user Lean repos harvest"        "$REPO/brain/ingest/lean_repo.py" --user-repos
    touch "$LOGDIR/.stamp.brain-weekly"
  else
    echo "(weekly sources not due — skipping lmfdb/eom/planetmath/descriptions/formal-conjectures/erdos/tauceti/user-repos)"
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
    echo "=== agent team: cartographer + linker + skeptic (writes brain/proposals/ only) ==="
    if [ ! -x "$AGENT_PY" ]; then
      echo "(! agent team skipped: SDK Python is missing or not executable: $AGENT_PY)"
    else
      retry_on_ratelimit "$AGENT_PY" "$REPO/brain/sync_agents.py" \
          --budget-tokens "$BRAIN_AGENT_BUDGET" \
          --repo-modules "${WIKILEAN_BRAIN_REPO_MODULES:-8}" \
        || echo "(sync_agents returned $? — proposals may be partial; the fold gates everything)"
    fi
  else
    echo "(agent team disabled — WIKILEAN_BRAIN_AGENTS=0)"
  fi
  echo

  # ---- 3. FOLD + BUILD (abort publish on failure, keep old shards) -----------
  PUBLISH_OK=1
  echo "=== fold proposals (deterministic verifier; network: Wikidata) ==="
  if [ "$PUBLISH_OK" = "1" ] && "$PYTHON_BIN" "$REPO/brain/fold_proposals.py"; then
    echo "(fold GREEN)"
  elif [ "$PUBLISH_OK" = "1" ]; then
    echo "!!! fold_proposals FAILED — build and publish aborted; see fold output above"
    PUBLISH_OK=0
  fi
  echo
  echo "=== rebuild brain graph (rollups are pinned — not rebuilt nightly) ==="
  if [ "$PUBLISH_OK" = "1" ] && ! "$PYTHON_BIN" "$REPO/brain/build_snapshot.py"; then
    echo "!!! build_snapshot FAILED — publish aborted (previous complete snapshot retained)"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if "$PYTHON_BIN" "$REPO/brain/test_acceptance.py"; then
      echo "(acceptance GREEN)"
    else
      echo "!!! test_acceptance RED — publish aborted, old shards stay live"
      PUBLISH_OK=0
    fi
  fi
  if [ "$PUBLISH_OK" = "1" ] && ! "$PYTHON_BIN" "$REPO/brain/build_shards.py"; then
    echo "!!! build_shards FAILED — publish aborted, old shards stay live"
    PUBLISH_OK=0
  fi

  # ---- the v3 ATOM layer (brain/SCHEMA.md#v3) --------------------------------
  # Organs -> cells -> supercells -> synapses, then the cell shards the client
  # reads. Same discipline as above: acceptance RED aborts the publish and the old
  # shards stay live. build_cells runs the force layout (~3min), which is why the
  # client no longer simulates anything.
  if [ "$PUBLISH_OK" = "1" ] && ! "$PYTHON_BIN" "$REPO/brain/build_cells.py"; then
    echo "!!! build_cells FAILED — publish aborted (old cells.jsonl intact)"
    PUBLISH_OK=0
  fi
  # Add the freshly built cell/synapse layer to the generated local index. This
  # imports existing JSONL only; it never rewrites the tracked dataset.
  if [ "$PUBLISH_OK" = "1" ] \
      && ! "$PYTHON_BIN" "$REPO/brain/build_snapshot.py" --from-jsonl; then
    echo "!!! derived SQLite refresh FAILED — publish aborted (previous complete index retained)"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if "$PYTHON_BIN" "$REPO/brain/test_cells.py"; then
      echo "(cell acceptance GREEN)"
    else
      echo "!!! test_cells RED — publish aborted, old cell shards stay live"
      PUBLISH_OK=0
    fi
  fi
  # ---- the FRONTIER layer (brain/SCHEMA.md "Frontier layer") -----------------
  # Partitions the homeless (decl-less) cells into named frontier areas
  # (brain/data/frontier.jsonl); build_cell_shards folds the rows into
  # supercells.json so the "no formal home" blob drains into browsable
  # territories. Deterministic + cheap (~2s). A failure aborts the publish:
  # shards cut against a stale frontier would drop or double-place cells, which
  # test_frontier below would catch anyway — abort here with the honest message.
  if [ "$PUBLISH_OK" = "1" ] && ! "$PYTHON_BIN" "$REPO/brain/build_frontier.py"; then
    echo "!!! build_frontier FAILED — publish aborted (old frontier.jsonl intact)"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ] && ! "$PYTHON_BIN" "$REPO/brain/build_cell_shards.py"; then
    echo "!!! build_cell_shards FAILED — publish aborted, old cell shards stay live"
    PUBLISH_OK=0
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if "$PYTHON_BIN" "$REPO/brain/test_cell_shards.py"; then
      echo "(cell shard acceptance GREEN)"
    else
      echo "!!! test_cell_shards RED — publish aborted, old cell shards stay live"
      PUBLISH_OK=0
    fi
  fi
  if [ "$PUBLISH_OK" = "1" ]; then
    if "$PYTHON_BIN" "$REPO/brain/test_frontier.py"; then
      echo "(frontier acceptance GREEN — the homeless partition holds)"
    else
      echo "!!! test_frontier RED — publish aborted, old cell shards stay live"
      PUBLISH_OK=0
    fi
  fi
  # ---- the /brain PAGE (site/build_brain_page.py -> site/out/brain.html) ------
  # Build the page under the same gate as its data. The release builder freezes
  # this exact file and the public stager activates it with the matching immutable
  # namespace; mutable site/out is never copied directly into the deployment.
  if [ "$PUBLISH_OK" = "1" ]; then
    if "$PYTHON_BIN" "$REPO/site/build_brain_page.py" && [ -s "$REPO/site/out/brain.html" ] \
        && "$PYTHON_BIN" "$REPO/site/test_frontier_page.py"; then
      echo "(brain page rebuilt and checked: $(wc -c <"$REPO/site/out/brain.html" | tr -d ' ') B)"
    else
      echo "!!! build_brain_page or Frontier page contract FAILED — publish aborted, the live page stays"
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

  # ---- 4. RELEASE (freeze -> verify -> stage -> test exact bytes) -------------
  RELEASE_ID=""
  RELEASE_HEX=""
  RELEASE_ROOT=""
  RELEASE_MANIFEST=""
  PREDEPLOY_DIR=""
  PREDEPLOY_STATUS_BEFORE=""
  PREDEPLOY_STATUS_AFTER=""
  PREDEPLOY_SELECTOR=""
  FINAL_SELECTOR=""
  PREDEPLOY_VERSION=""
  PREDEPLOY_RELEASE_ID=""
  DEPLOY_OUTPUT=""
  FINAL_PUBLIC_OUTPUT=""
  FINAL_PUBLIC_RESULT=""
  DEPLOYED_VERSION=""
  POSTDEPLOY_STATUS=""
  POSTCANARY_STATUS=""
  ROLLBACK_STATUS_BEFORE=""
  ROLLBACK_STATUS_AFTER=""
  ROLLBACK_STATUS_FINAL=""
  ROLLBACK_SELECTOR=""
  RETAINED_RELEASE_ID=""
  RETAINED_RELEASE_ROOT=""
  RETAINED_RELEASE_MANIFEST=""
  BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  GITDIR="$(git -C "$REPO" rev-parse --git-dir 2>/dev/null)"
  case "$GITDIR" in /*) ;; *) GITDIR="$REPO/$GITDIR" ;; esac
  # `npm run deploy` bundles all Worker sources, configuration, and staged static
  # assets. Keep that original broad boundary, then add the release construction
  # sources outside wiki/. Generated site/out releases and ignored wiki/public
  # bytes do not appear in this source-authority check.
  REDUCER_SCOPE="$("$PYTHON_BIN" - "$REPO/brain/authority/reducer-inputs-v1.json" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
scope = value.get("scope") if isinstance(value, dict) else None
if not isinstance(scope, list) or not scope or not all(isinstance(path, str) and path for path in scope):
    raise SystemExit("invalid reducer inventory scope")
print("\n".join(scope))
PY
)" || {
    echo "!!! RELEASE ABORTED: reducer inventory scope is invalid"
    PUBLISH_OK=0
    REDUCER_SCOPE=""
  }
  DIRTY=""
  if ! DIRTY="$(git -C "$REPO" status --porcelain -- \
      wiki/ site/assets \
      brain/tools/build_release.py brain/tools/verify_release.py brain/tools/measure_store.py \
      brain/tools/authority_contracts.py brain/authority/schemas \
      brain/authority/reducer-inputs-v1.json \
      $REDUCER_SCOPE \
      site/test_frontier_page.py \
      site/ops/brain-nightly.sh site/ops/brain-canary.py site/ops/nightly.env)"; then
    echo "!!! RELEASE ABORTED: could not inspect release-affecting source status"
    PUBLISH_OK=0
  fi

  if [ "$PUBLISH_OK" = "1" ] && [ ! -s "$REPO/site/out/brain.html" ]; then
    echo "!!! RELEASE ABORTED: site/out/brain.html is missing/empty"
    PUBLISH_OK=0
  elif [ "$PUBLISH_OK" = "1" ] && [ -n "$DIRTY" ]; then
    echo "!!! RELEASE ABORTED: release-affecting source is uncommitted; the Git authority would be false:"
    echo "$DIRTY"
    PUBLISH_OK=0
  elif [ "$PUBLISH_OK" = "1" ] && { [ ! -f "$REPO/brain/tools/build_release.py" ] || [ ! -f "$REPO/wiki/scripts/brain-release-public.ts" ]; }; then
    echo "!!! RELEASE ABORTED: release builder or public staging module is missing"
    PUBLISH_OK=0
  fi

  if [ "$PUBLISH_OK" = "1" ]; then
    echo "=== release: freeze verified content-addressed candidate ==="
    mkdir -p "$RELEASE_STORE"
    TMP_BUILD_OUTPUT="$(mktemp "$LOGDIR/.brain-release-build.XXXXXX")"
    TMP_RELEASE_RESULT="$(mktemp "$REPO/site/out/.brain-release-result.XXXXXX")"
    AUTHORITY_COMMIT="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"
    CONFIG_SHA="$(printf '%s\n' \
      "semantic_epoch=$BRAIN_SEMANTIC_EPOCH" \
      "schedule=$BRAIN_REDUCER_SCHEDULE" \
      "reducer_version=$BRAIN_REDUCER_VERSION" \
      "BRAIN_EXT_NODE_CAP=${BRAIN_EXT_NODE_CAP:-}" | sha256_text)"
    ENV_SHA="$(printf '%s\n' \
      "python=$("$PYTHON_BIN" --version 2>&1)" \
      "platform=$(uname -srm)" \
      "LC_ALL=${LC_ALL:-}" \
      "LANG=${LANG:-}" | sha256_text)"
    if "$PYTHON_BIN" "$REPO/brain/tools/build_release.py" \
        --repo-root "$REPO" \
        --output-store "$RELEASE_STORE" \
        --semantic-epoch "$BRAIN_SEMANTIC_EPOCH" \
        --schedule "$BRAIN_REDUCER_SCHEDULE" \
        --reducer-version "$BRAIN_REDUCER_VERSION" \
        --authority-git-commit "$AUTHORITY_COMMIT" \
        --reducer-git-commit "$AUTHORITY_COMMIT" \
        --configuration-sha256 "$CONFIG_SHA" \
        --environment-sha256 "$ENV_SHA" \
        --input-inventory "brain/authority/reducer-inputs-v1.json" \
        >"$TMP_BUILD_OUTPUT" \
      && extract_json_object "$TMP_BUILD_OUTPUT" "$TMP_RELEASE_RESULT" \
           release_id release root manifest artifact_count byte_count; then
      mv "$TMP_RELEASE_RESULT" "$RELEASE_RESULT"
      RELEASE_ID="$(json_field "$RELEASE_RESULT" release_id)"
      RELEASE_HEX="$(json_field "$RELEASE_RESULT" release)"
      RELEASE_ROOT="$(json_field "$RELEASE_RESULT" root)"
      RELEASE_MANIFEST="$(json_field "$RELEASE_RESULT" manifest)"
      echo "(release frozen: $RELEASE_ID)"
    else
      echo "!!! RELEASE ABORTED: build_release.py failed or emitted no valid result"
      [ -s "$TMP_BUILD_OUTPUT" ] && cat "$TMP_BUILD_OUTPUT"
      PUBLISH_OK=0
    fi
    rm -f "$TMP_BUILD_OUTPUT" "$TMP_RELEASE_RESULT"
  fi

  if [ "$PUBLISH_OK" = "1" ]; then
    case "$RELEASE_ROOT" in "$RELEASE_STORE"/*) ;; *)
      echo "!!! RELEASE ABORTED: builder root escaped the configured release store: $RELEASE_ROOT"
      PUBLISH_OK=0
    esac
  fi
  if [ "$PUBLISH_OK" = "1" ] \
      && { [ "$RELEASE_ROOT" != "$RELEASE_STORE/$RELEASE_HEX" ] \
        || [ "$RELEASE_MANIFEST" != "$RELEASE_ROOT/release.json" ]; }; then
    echo "!!! RELEASE ABORTED: builder result paths do not match release identity"
    PUBLISH_OK=0
  fi

  if [ "$PUBLISH_OK" = "1" ]; then
    echo "=== release: independent frozen-byte verification ==="
    if "$PYTHON_BIN" "$REPO/brain/tools/verify_release.py" \
        --manifest "$RELEASE_MANIFEST" --root "$RELEASE_ROOT"; then
      echo "(independent release verification GREEN)"
    else
      echo "!!! RELEASE ABORTED: verify_release.py rejected the frozen release"
      PUBLISH_OK=0
    fi
  fi

  if [ "$PUBLISH_OK" = "1" ]; then
    echo "=== release: collect immutable SQLite and artifact metrics ==="
    TMP_METRICS_OUTPUT="$(mktemp "$LOGDIR/.brain-release-metrics.XXXXXX")"
    TMP_METRICS_RESULT="$(mktemp "$REPO/site/out/.brain-release-metrics.XXXXXX")"
    if "$PYTHON_BIN" "$REPO/brain/tools/measure_store.py" \
        --database "$RELEASE_ROOT/brain/data/brain.sqlite3" \
        --release-manifest "$RELEASE_MANIFEST" \
        --limit 100 --iterations 5 --warmup 1 --check-limit 100 \
        >"$TMP_METRICS_OUTPUT" \
      && extract_json_object "$TMP_METRICS_OUTPUT" "$TMP_METRICS_RESULT" \
           schema ok identity database counts analyze checks queries warnings duration_ms max_rss_bytes \
      && STORE_METRICS_RELEASE_ID="$(store_metrics_release_id "$TMP_METRICS_RESULT" 2>/dev/null)" \
      && [ "$STORE_METRICS_RELEASE_ID" = "$RELEASE_ID" ]; then
      mv "$TMP_METRICS_RESULT" "$RELEASE_METRICS_RESULT"
      cat "$RELEASE_METRICS_RESULT"
      echo "(release metrics recorded: $RELEASE_METRICS_RESULT)"
    else
      echo "!!! RELEASE ABORTED: immutable release metrics failed or named the wrong release"
      [ -s "$TMP_METRICS_OUTPUT" ] && cat "$TMP_METRICS_OUTPUT"
      PUBLISH_OK=0
    fi
    rm -f "$TMP_METRICS_OUTPUT" "$TMP_METRICS_RESULT"
  fi

  # Deployment-enabled staging must retain the production release, not a local
  # selector left by a shadow run. A genuine first compatibility deployment may
  # return 404 because no selector exists yet; every other fetch failure aborts.
  if [ "$PUBLISH_OK" = "1" ] && [ "$BRAIN_DEPLOY" = "1" ]; then
    echo "=== release: record production selector before staging ==="
    PREDEPLOY_DIR="$(mktemp -d "$LOGDIR/.brain-predeploy.XXXXXX")"
    PREDEPLOY_STATUS_BEFORE="$PREDEPLOY_DIR/wrangler-status-before.json"
    PREDEPLOY_STATUS_AFTER="$PREDEPLOY_DIR/wrangler-status-after.json"
    PREDEPLOY_SELECTOR="$PREDEPLOY_DIR/current.json"
    FINAL_SELECTOR="$PREDEPLOY_DIR/current-final.json"
    DEPLOY_OUTPUT="$PREDEPLOY_DIR/deploy-output.txt"
    FINAL_PUBLIC_OUTPUT="$PREDEPLOY_DIR/public-final-output.txt"
    FINAL_PUBLIC_RESULT="$PREDEPLOY_DIR/public-final-result.json"
    POSTDEPLOY_STATUS="$PREDEPLOY_DIR/wrangler-status-postdeploy.json"
    POSTCANARY_STATUS="$PREDEPLOY_DIR/wrangler-status-postcanary.json"
    ROLLBACK_STATUS_BEFORE="$PREDEPLOY_DIR/wrangler-status-rollback-before.json"
    ROLLBACK_STATUS_AFTER="$PREDEPLOY_DIR/wrangler-status-rollback-after.json"
    ROLLBACK_STATUS_FINAL="$PREDEPLOY_DIR/wrangler-status-rollback-final.json"
    ROLLBACK_SELECTOR="$PREDEPLOY_DIR/current-rollback.json"
    SELECTOR_HTTP="$(curl -sS --max-time 30 -H 'Cache-Control: no-cache' \
      -o "$PREDEPLOY_SELECTOR" -w '%{http_code}' \
      "$BRAIN_CANARY_URL/assets/brain/current.json?__brain_predeploy=$TS" || true)"
    if [ "$SELECTOR_HTTP" = "200" ]; then
      if RELEASE_LINES="$(selector_release_ids "$PREDEPLOY_SELECTOR" "$RELEASE_ID" 2>/dev/null)"; then
        PREDEPLOY_RELEASE_ID="$(printf '%s\n' "$RELEASE_LINES" | head -n 1)"
        RETAINED_RELEASE_ID="$(printf '%s\n' "$RELEASE_LINES" | tail -n 1)"
        [ "$RETAINED_RELEASE_ID" = "-" ] && RETAINED_RELEASE_ID=""
        echo "(predeploy Brain release: $PREDEPLOY_RELEASE_ID)"
      else
        echo "!!! RELEASE ABORTED: production selector is malformed"
        PUBLISH_OK=0
      fi
    elif [ "$SELECTOR_HTTP" = "404" ]; then
      echo "(no production Brain selector yet; treating this as the first compatibility deployment)"
    else
      echo "!!! RELEASE ABORTED: production selector fetch returned HTTP ${SELECTOR_HTTP:-network-error}"
      PUBLISH_OK=0
    fi
  fi

  if [ "$PUBLISH_OK" = "1" ] && [ -n "$RETAINED_RELEASE_ID" ]; then
    RETAINED_RELEASE_ROOT="$RELEASE_STORE/${RETAINED_RELEASE_ID#sha256:}"
    RETAINED_RELEASE_MANIFEST="$RETAINED_RELEASE_ROOT/release.json"
    if [ ! -f "$RETAINED_RELEASE_MANIFEST" ] \
        || ! "$PYTHON_BIN" "$REPO/brain/tools/verify_release.py" \
          --manifest "$RETAINED_RELEASE_MANIFEST" --root "$RETAINED_RELEASE_ROOT"; then
      echo "!!! RELEASE ABORTED: qualified production release is not available as a verified frozen release"
      echo "    expected: $RETAINED_RELEASE_MANIFEST"
      PUBLISH_OK=0
    fi
  fi

  if [ "$PUBLISH_OK" = "1" ]; then
    echo "=== release: atomic public staging (current + production previous + aliases) ==="
    STAGE_ARGS=(
      --brain-release-manifest "$RELEASE_MANIFEST"
      --brain-release-dir "$RELEASE_ROOT"
    )
    if [ -n "$RETAINED_RELEASE_ID" ]; then
      STAGE_ARGS+=(
        --brain-previous-release-manifest "$RETAINED_RELEASE_MANIFEST"
        --brain-previous-release-dir "$RETAINED_RELEASE_ROOT"
      )
    elif [ "$BRAIN_DEPLOY" = "1" ]; then
      # Explicit current-as-previous tells the stager there is no retained
      # production release; it normalizes the duplicate away and does not infer
      # a previous namespace from local shadow-run history.
      STAGE_ARGS+=(
        --brain-previous-release-manifest "$RELEASE_MANIFEST"
        --brain-previous-release-dir "$RELEASE_ROOT"
      )
    fi
    TMP_PUBLIC_OUTPUT="$(mktemp "$LOGDIR/.brain-public-build.XXXXXX")"
    TMP_PUBLIC_RESULT="$(mktemp "$REPO/site/out/.brain-public-result.XXXXXX")"
    if (cd "$REPO/wiki" \
          && node --experimental-strip-types scripts/build-public.ts "${STAGE_ARGS[@]}" \
            >"$TMP_PUBLIC_OUTPUT") \
        && extract_json_object "$TMP_PUBLIC_OUTPUT" "$TMP_PUBLIC_RESULT" \
             schema public_dir mathlib_declarations brain duration_ms max_rss_bytes \
        && PUBLIC_STAGE_RELEASE_ID="$(public_result_release_id "$TMP_PUBLIC_RESULT" 2>/dev/null)" \
        && [ "$PUBLIC_STAGE_RELEASE_ID" = "$RELEASE_ID" ] \
        && cmp -s "$RELEASE_ROOT/site/out/brain.html" "$REPO/wiki/public/brain.html"; then
      mv "$TMP_PUBLIC_RESULT" "$PUBLIC_RESULT"
      cat "$PUBLIC_RESULT"
      echo "(public release staging GREEN; metrics recorded and brain.html matches frozen release)"
    else
      echo "!!! RELEASE ABORTED: verified public staging failed, named the wrong release, or brain.html differs"
      [ -s "$TMP_PUBLIC_OUTPUT" ] && cat "$TMP_PUBLIC_OUTPUT"
      PUBLISH_OK=0
    fi
    rm -f "$TMP_PUBLIC_OUTPUT" "$TMP_PUBLIC_RESULT"
  fi

  if [ "$PUBLISH_OK" = "1" ]; then
    echo "=== release: Worker checks against staged bytes ==="
    if (cd "$REPO/wiki" && npm run typecheck && npm run test:unit); then
      echo "(Worker typecheck + unit tests GREEN against staged release)"
    else
      echo "!!! RELEASE ABORTED: Worker checks failed; production unchanged"
      PUBLISH_OK=0
    fi
  fi

  # ---- 5. DEPLOY + CANARY + GUARDED ROLLBACK -------------------------------
  if [ "$PUBLISH_OK" = "1" ] && [ "$BRAIN_DEPLOY" = "1" ]; then
    DEPLOY_READY=1
    # wiki/public is generated and ignored, so Git cannot protect it. Restage
    # the exact frozen release after all slow checks. The tracked-source and
    # live-state fences follow this last mutating preparation step.
    if [ "$DEPLOY_READY" = "1" ]; then
      echo "=== deploy: final frozen-byte public restage ==="
      if (cd "$REPO/wiki" \
            && node --experimental-strip-types scripts/build-public.ts "${STAGE_ARGS[@]}" \
              >"$FINAL_PUBLIC_OUTPUT") \
          && extract_json_object "$FINAL_PUBLIC_OUTPUT" "$FINAL_PUBLIC_RESULT" \
               schema public_dir mathlib_declarations brain duration_ms max_rss_bytes \
          && FINAL_STAGE_RELEASE_ID="$(public_result_release_id "$FINAL_PUBLIC_RESULT" 2>/dev/null)" \
          && [ "$FINAL_STAGE_RELEASE_ID" = "$RELEASE_ID" ] \
          && cmp -s "$RELEASE_ROOT/site/out/brain.html" "$REPO/wiki/public/brain.html"; then
        mv "$FINAL_PUBLIC_RESULT" "$PUBLIC_RESULT"
        echo "(final frozen-byte public restage GREEN: $RELEASE_ID)"
      else
        echo "!!! SKIPPED-DEPLOY: final frozen-byte public restage failed"
        [ -s "$FINAL_PUBLIC_OUTPUT" ] && cat "$FINAL_PUBLIC_OUTPUT"
        DEPLOY_READY=0
      fi
      rm -f "$FINAL_PUBLIC_OUTPUT" "$FINAL_PUBLIC_RESULT"
    fi

    echo "=== deploy: final source-authority recheck ==="
    FINAL_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    FINAL_HEAD="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    FINAL_DIRTY=""
    if ! FINAL_DIRTY="$(git -C "$REPO" status --porcelain -- \
        wiki/ site/assets \
        brain/tools/build_release.py brain/tools/verify_release.py brain/tools/measure_store.py \
        brain/tools/authority_contracts.py brain/authority/schemas \
        brain/authority/reducer-inputs-v1.json \
        $REDUCER_SCOPE \
        site/test_frontier_page.py \
        site/ops/brain-nightly.sh site/ops/brain-canary.py site/ops/nightly.env)"; then
      echo "!!! SKIPPED-DEPLOY: could not inspect final release-affecting source status"
      DEPLOY_READY=0
    fi
    if [ "$DEPLOY_READY" = "1" ] && [ "$FINAL_BRANCH" != "main" ]; then
      echo "!!! SKIPPED-DEPLOY: checked-out branch is '${FINAL_BRANCH:-unknown}', not main"
      DEPLOY_READY=0
    elif [ "$DEPLOY_READY" = "1" ] && [ "$FINAL_HEAD" != "$AUTHORITY_COMMIT" ]; then
      echo "!!! SKIPPED-DEPLOY: HEAD changed after release freeze ($AUTHORITY_COMMIT -> ${FINAL_HEAD:-unknown})"
      DEPLOY_READY=0
    elif [ "$DEPLOY_READY" = "1" ] && [ -n "$FINAL_DIRTY" ]; then
      echo "!!! SKIPPED-DEPLOY: release-affecting source changed after release freeze:"
      echo "$FINAL_DIRTY"
      DEPLOY_READY=0
    elif [ "$DEPLOY_READY" = "1" ] \
        && { [ -d "$GITDIR/rebase-merge" ] || [ -d "$GITDIR/rebase-apply" ] || [ -f "$GITDIR/MERGE_HEAD" ]; }; then
      echo "!!! SKIPPED-DEPLOY: rebase/merge in progress ($GITDIR)"
      DEPLOY_READY=0
    fi

    # Record production as status A -> exact selector -> status A. If either
    # changed after the final restage and source check, another
    # operator/deployer owns this window. No mutating preparation step is
    # allowed between these fences and the Wrangler invocation.
    if [ "$DEPLOY_READY" = "1" ]; then
      echo "=== deploy: stable prestate sandwich ==="
      if ! (cd "$REPO/wiki" \
          && npx --no-install wrangler deployments status --json >"$PREDEPLOY_STATUS_BEFORE"); then
        echo "!!! SKIPPED-DEPLOY: could not read predeploy Worker status"
        DEPLOY_READY=0
      else
        VERSION_BEFORE="$(wrangler_sole_version "$PREDEPLOY_STATUS_BEFORE" 2>/dev/null || true)"
        if [ -z "$VERSION_BEFORE" ]; then
          echo "!!! SKIPPED-DEPLOY: predeploy traffic is not one unambiguous 100% Worker version"
          DEPLOY_READY=0
        fi
      fi
    fi
    if [ "$DEPLOY_READY" = "1" ]; then
      FINAL_SELECTOR_HTTP="$(curl -sS --max-time 30 -H 'Cache-Control: no-cache' \
        -o "$FINAL_SELECTOR" -w '%{http_code}' \
        "$BRAIN_CANARY_URL/assets/brain/current.json?__brain_predeploy=${TS}-final" || true)"
      if [ "$FINAL_SELECTOR_HTTP" != "$SELECTOR_HTTP" ] \
          || { [ "$SELECTOR_HTTP" = "200" ] && ! cmp -s "$PREDEPLOY_SELECTOR" "$FINAL_SELECTOR"; }; then
        echo "!!! SKIPPED-DEPLOY: production selector changed during staging/checks"
        DEPLOY_READY=0
      elif [ "$FINAL_SELECTOR_HTTP" != "200" ] && [ "$FINAL_SELECTOR_HTTP" != "404" ]; then
        echo "!!! SKIPPED-DEPLOY: final production selector fetch returned HTTP ${FINAL_SELECTOR_HTTP:-network-error}"
        DEPLOY_READY=0
      fi
    fi
    if [ "$DEPLOY_READY" = "1" ]; then
      if ! (cd "$REPO/wiki" \
          && npx --no-install wrangler deployments status --json >"$PREDEPLOY_STATUS_AFTER"); then
        echo "!!! SKIPPED-DEPLOY: could not re-read predeploy Worker status"
        DEPLOY_READY=0
      else
        VERSION_AFTER="$(wrangler_sole_version "$PREDEPLOY_STATUS_AFTER" 2>/dev/null || true)"
        if [ -z "$VERSION_AFTER" ] || [ "$VERSION_AFTER" != "$VERSION_BEFORE" ]; then
          echo "!!! SKIPPED-DEPLOY: Worker version changed during the prestate sandwich"
          DEPLOY_READY=0
        else
          PREDEPLOY_VERSION="$VERSION_BEFORE"
          echo "(stable predeploy Worker version: $PREDEPLOY_VERSION)"
        fi
      fi
    fi

    DEPLOY_ATTEMPTED=0
    DEPLOY_SUCCEEDED=0
    ROLLBACK_ELIGIBLE=0
    if [ "$DEPLOY_READY" = "1" ]; then
      DEPLOY_TAG="brain-${RELEASE_HEX:0:12}"
      echo "=== deploy: one strict Wrangler deployment ($DEPLOY_TAG) ==="
      DEPLOY_ATTEMPTED=1
      if (cd "$REPO/wiki" && npm run deploy -- --strict --tag "$DEPLOY_TAG" \
          --message "Brain release $RELEASE_ID ($TS)" >"$DEPLOY_OUTPUT" 2>&1); then
        DEPLOY_SUCCEEDED=1
        cat "$DEPLOY_OUTPUT"
        DEPLOYED_VERSION="$(wrangler_deployed_version "$DEPLOY_OUTPUT" 2>/dev/null || true)"
        if [ -z "$DEPLOYED_VERSION" ]; then
          echo "!!! DEPLOY STATE UNKNOWN: Wrangler succeeded but emitted no unique candidate version ID"
          echo "    The release canary will still run; automatic rollback is disabled for this run."
          RUN_STATUS=1
        elif wait_for_worker_version "$DEPLOYED_VERSION" "$POSTDEPLOY_STATUS"; then
          ROLLBACK_ELIGIBLE=1
          echo "(candidate Worker version owns 100% traffic: $DEPLOYED_VERSION)"
        else
          echo "!!! DEPLOY CONTROL PLANE DID NOT CONVERGE to candidate $DEPLOYED_VERSION"
          echo "    The release canary will still run; automatic rollback is disabled for this run."
          RUN_STATUS=1
        fi
      else
        [ -s "$DEPLOY_OUTPUT" ] && cat "$DEPLOY_OUTPUT"
        echo "!!! DEPLOY COMMAND FAILED: Wrangler returned nonzero; remote state is uncertain"
        echo "    The release canary will still run; automatic rollback is disabled for this run."
        RUN_STATUS=1
      fi
    fi

    if [ "$DEPLOY_ATTEMPTED" = "1" ]; then
      echo "(polling for release convergence)"
      if run_release_canary "$RELEASE_ID"; then
        echo "(release-content canary GREEN: $RELEASE_ID)"
        if [ "$DEPLOY_SUCCEEDED" != "1" ]; then
          echo "(! release content converged despite a failed deploy command; inspect the control plane manually)"
        fi
        if [ -n "$DEPLOYED_VERSION" ]; then
          if wait_for_worker_version "$DEPLOYED_VERSION" "$POSTCANARY_STATUS"; then
            echo "(post-deploy control plane GREEN: $DEPLOYED_VERSION)"
          else
            echo "!!! POST-CANARY STATE CHANGED: candidate no longer exclusively owns traffic"
            RUN_STATUS=1
          fi
        fi
      else
        echo "!!! POST-DEPLOY CANARY FAILED for $RELEASE_ID"
        RUN_STATUS=1
        if [ "$BRAIN_AUTO_ROLLBACK" != "1" ]; then
          echo "(! automatic rollback disabled by default; production was not overwritten again)"
          echo "    Inspect status and selector, then follow docs/BRAIN-RELEASE-RUNBOOK.md"
        elif [ "$ROLLBACK_ELIGIBLE" != "1" ]; then
          echo "!!! automatic rollback skipped: candidate Worker version was never established"
        else
          echo "=== rollback: guarded opt-in restoration of $PREDEPLOY_VERSION ==="
          ROLLBACK_GUARD=1
          if ! (cd "$REPO/wiki" \
              && npx --no-install wrangler deployments status --json >"$ROLLBACK_STATUS_BEFORE") \
              || [ "$(wrangler_sole_version "$ROLLBACK_STATUS_BEFORE" 2>/dev/null || true)" != "$DEPLOYED_VERSION" ]; then
            echo "!!! automatic rollback skipped: candidate no longer exclusively owns traffic"
            ROLLBACK_GUARD=0
          fi
          if [ "$ROLLBACK_GUARD" = "1" ]; then
            ROLLBACK_SELECTOR_HTTP="$(curl -sS --max-time 30 -H 'Cache-Control: no-cache' \
              -o "$ROLLBACK_SELECTOR" -w '%{http_code}' \
              "$BRAIN_CANARY_URL/assets/brain/current.json?__brain_rollback=$TS" || true)"
            ROLLBACK_CURRENT_RELEASE=""
            if [ "$ROLLBACK_SELECTOR_HTTP" = "200" ]; then
              ROLLBACK_LINES="$(selector_release_ids "$ROLLBACK_SELECTOR" "$RELEASE_ID" 2>/dev/null || true)"
              ROLLBACK_CURRENT_RELEASE="$(printf '%s\n' "$ROLLBACK_LINES" | head -n 1)"
            fi
            if [ "$ROLLBACK_CURRENT_RELEASE" != "$RELEASE_ID" ]; then
              echo "!!! automatic rollback skipped: live selector is not the failed candidate"
              ROLLBACK_GUARD=0
            fi
          fi
          if [ "$ROLLBACK_GUARD" = "1" ]; then
            if ! (cd "$REPO/wiki" \
                && npx --no-install wrangler deployments status --json >"$ROLLBACK_STATUS_AFTER") \
                || [ "$(wrangler_sole_version "$ROLLBACK_STATUS_AFTER" 2>/dev/null || true)" != "$DEPLOYED_VERSION" ]; then
              echo "!!! automatic rollback skipped: Worker version changed during rollback guard"
              ROLLBACK_GUARD=0
            fi
          fi
          if [ "$ROLLBACK_GUARD" = "1" ]; then
            echo "(! Wrangler rollback has no compare-and-swap; use this opt-in only under an exclusive deploy window)"
            if (cd "$REPO/wiki" && npx --no-install wrangler rollback "$PREDEPLOY_VERSION" --yes \
                --message "Automatic rollback after Brain canary failure for $RELEASE_ID") \
                && wait_for_worker_version "$PREDEPLOY_VERSION" "$ROLLBACK_STATUS_FINAL"; then
              echo "(rollback command restored the recorded Worker version)"
              if [ -n "$PREDEPLOY_RELEASE_ID" ]; then
                if run_release_canary "$PREDEPLOY_RELEASE_ID"; then
                  echo "(rollback convergence GREEN: $PREDEPLOY_RELEASE_ID)"
                else
                  echo "!!! rollback restored the Worker version but prior release did not converge"
                fi
              else
                echo "(! rollback cannot be release-qualified: this was a first compatibility deployment)"
              fi
            else
              echo "!!! AUTOMATIC ROLLBACK FAILED OR DID NOT RESTORE THE RECORDED VERSION"
              echo "    Manual recovery: cd '$REPO/wiki' && npx --no-install wrangler deployments status --json"
            fi
          fi
        fi
      fi
    fi
    if [ "$DEPLOY_READY" != "1" ]; then
      RUN_STATUS=1
    fi
  elif [ "$PUBLISH_OK" = "1" ]; then
    echo "(deploy disabled — verified release $RELEASE_ID is staged locally; production unchanged)"
  else
    echo "!!! PUBLISH ABORTED — see the build/release failure above; production unchanged"
    RUN_STATUS=1
  fi
  if [ -n "$PREDEPLOY_DIR" ]; then
    rm -f \
      "$PREDEPLOY_STATUS_BEFORE" "$PREDEPLOY_STATUS_AFTER" \
      "$PREDEPLOY_SELECTOR" "$FINAL_SELECTOR" "$DEPLOY_OUTPUT" \
      "$POSTDEPLOY_STATUS" "$POSTCANARY_STATUS" \
      "$ROLLBACK_STATUS_BEFORE" "$ROLLBACK_STATUS_AFTER" \
      "$ROLLBACK_STATUS_FINAL" "$ROLLBACK_SELECTOR"
    rmdir "$PREDEPLOY_DIR" 2>/dev/null || true
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/brain-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
exit "$RUN_STATUS"
