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
#   5. DEPLOY is deliberately NOT performed here. Exact frozen releases are
#      promoted only by site/ops/brain-promote-release.sh; enabling the legacy
#      nightly deploy flag fails closed before ingest/build work.
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

# Preserve explicit values sealed into a rendered LaunchAgent plist. They must
# win over a stale local file when an operator deliberately supplies overrides.
LAUNCHD_PYTHON="${WIKILEAN_PYTHON:-}"
LAUNCHD_BRAIN_MATHLIB="${BRAIN_MATHLIB_CHECKOUT:-}"

# Editable tunables live in site/ops/nightly.env (sourced with ":=" so a
# one-off env override still wins). Host-local absolute paths belong in the
# gitignored nightly.local.env, sourced afterward. Missing files use defaults.
[ -f "$REPO/site/ops/nightly.env" ] && . "$REPO/site/ops/nightly.env"
[ -f "$REPO/site/ops/nightly.local.env" ] && . "$REPO/site/ops/nightly.local.env"
[ -n "$LAUNCHD_PYTHON" ] && WIKILEAN_PYTHON="$LAUNCHD_PYTHON"
[ -n "$LAUNCHD_BRAIN_MATHLIB" ] && BRAIN_MATHLIB_CHECKOUT="$LAUNCHD_BRAIN_MATHLIB"

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
if [ -n "$PYTHON_BIN" ] && [ "${PYTHON_BIN#/}" != "$PYTHON_BIN" ] \
    && [ -d "$(dirname -- "$PYTHON_BIN")" ]; then
  PYTHON_BIN="$(CDPATH= cd -- "$(dirname -- "$PYTHON_BIN")" && pwd -P)/$(basename -- "$PYTHON_BIN")"
fi
AGENT_PY="${WIKILEAN_BRAIN_AGENT_PYTHON:-$REPO/catalog/.venv/bin/python3}"

BRAIN_REFRESH="${WIKILEAN_BRAIN_REFRESH:-1}"
BRAIN_AGENTS="${WIKILEAN_BRAIN_AGENTS:-0}"
BRAIN_AGENT_BUDGET="${WIKILEAN_BRAIN_AGENT_BUDGET:-500000}"
BRAIN_DEPLOY="${WIKILEAN_BRAIN_DEPLOY:-0}"
BRAIN_MATHLIB_CHECKOUT="${BRAIN_MATHLIB_CHECKOUT:-}"
if [ -n "$BRAIN_MATHLIB_CHECKOUT" ] \
    && [ "${BRAIN_MATHLIB_CHECKOUT#/}" != "$BRAIN_MATHLIB_CHECKOUT" ] \
    && [ -d "$BRAIN_MATHLIB_CHECKOUT" ]; then
  BRAIN_MATHLIB_CHECKOUT="$(CDPATH= cd -- "$BRAIN_MATHLIB_CHECKOUT" && pwd -P)"
fi
if [ -n "$BRAIN_MATHLIB_CHECKOUT" ]; then
  export BRAIN_MATHLIB_CHECKOUT
else
  export -n BRAIN_MATHLIB_CHECKOUT 2>/dev/null || true
fi
BRAIN_SEMANTIC_EPOCH="${WIKILEAN_BRAIN_SEMANTIC_EPOCH:-brain-v3-current}"
BRAIN_REDUCER_SCHEDULE="${WIKILEAN_BRAIN_REDUCER_SCHEDULE:-brain-v3-current}"
BRAIN_REDUCER_VERSION="${WIKILEAN_BRAIN_REDUCER_VERSION:-1}"
WIKIDATA_ENTITY_STORE="$REPO/catalog/.cache/wikidata/entity-bundles"
RELEASE_STORE="$REPO/site/out/brain-releases"
RELEASE_RESULT="$REPO/site/out/brain-release-result.json"
RELEASE_METRICS_RESULT="$REPO/site/out/brain-release-metrics.json"
PUBLIC_RESULT="$REPO/site/out/brain-public-result.json"

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
# the 03:10/03:20 jobs. Never steal it based on age: an old timestamp does not
# prove that the owning process is dead.
LOCKDIR="$LOGDIR/.lock.brain.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "[$TS] brain lock exists; refusing age-only recovery — verify no process is active, then remove $LOCKDIR manually" >>"$LOGDIR/skips.log"
  exit 1
fi
RUN_DIR=""
cleanup() {
  cleanup_status=$?
  trap - EXIT
  if [ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ]; then
    rm -f "$RUN_DIR/wikidata-request-plan.json" \
          "$RUN_DIR/wikidata-acquire.stdout"
    if ! rmdir "$RUN_DIR" 2>/dev/null; then
      printf 'brain-nightly: private run directory not empty; inspect manually: %s\n' \
        "$RUN_DIR" >&2
    fi
  fi
  rmdir "$LOCKDIR" 2>/dev/null || true
  exit "$cleanup_status"
}
trap cleanup EXIT

# A new enabled run invalidates every prior machine-readable result immediately,
# even if runtime/input preflight fails or the process is interrupted later.
mkdir -p "$REPO/site/out"
rm -f "$RELEASE_RESULT" "$RELEASE_METRICS_RESULT" "$PUBLIC_RESULT"
if [ "$BRAIN_DEPLOY" != "0" ]; then
  {
    echo "=== WikiLean nightly BRAIN refresh $TS ==="
    echo "!!! WIKILEAN_BRAIN_DEPLOY is retired for the nightly rebuild path"
    echo "    Promote an exact frozen release with site/ops/brain-promote-release.sh"
  } >>"$LOG" 2>&1
  exit 1
fi
if [ ! -x "$PYTHON_BIN" ] \
    || [ "${PYTHON_BIN#/}" = "$PYTHON_BIN" ] \
    || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  {
    echo "=== WikiLean nightly BRAIN refresh $TS ==="
    echo "!!! Python 3.12+ is required (selected ${PYTHON_BIN:-none})"
  } >>"$LOG" 2>&1
  exit 1
fi
if [ -z "$BRAIN_MATHLIB_CHECKOUT" ] \
    || [ "${BRAIN_MATHLIB_CHECKOUT#/}" = "$BRAIN_MATHLIB_CHECKOUT" ] \
    || [ ! -d "$BRAIN_MATHLIB_CHECKOUT/Algebra" ]; then
  {
    echo "=== WikiLean nightly BRAIN refresh $TS ==="
    echo "!!! BRAIN_MATHLIB_CHECKOUT must name a readable mathlib4/Mathlib directory"
    echo "    configured value: ${BRAIN_MATHLIB_CHECKOUT:-unset}"
  } >>"$LOG" 2>&1
  exit 1
fi
case "$WIKIDATA_ENTITY_STORE" in
  /*) ;;
  *)
    {
      echo "=== WikiLean nightly BRAIN refresh $TS ==="
      echo "!!! derived Wikidata entity store must be an absolute path"
      echo "    configured value: ${WIKIDATA_ENTITY_STORE:-unset}"
    } >>"$LOG" 2>&1
    exit 1
    ;;
esac
if ! RUN_DIR="$(mktemp -d "$LOGDIR/.brain-run.XXXXXX")"; then
  echo "[$TS] could not create private Brain run directory" >>"$LOG"
  exit 1
fi
chmod 0700 "$RUN_DIR" || {
  echo "[$TS] could not secure private Brain run directory $RUN_DIR" >>"$LOG"
  exit 1
}
if ! "$PYTHON_BIN" - "$RUN_DIR" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = path.lstat()
if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit("run directory is not a real directory")
if stat.S_IMODE(metadata.st_mode) != 0o700:
    raise SystemExit("run directory does not have mode 0700")
PY
then
  echo "[$TS] private Brain run directory validation failed: $RUN_DIR" >>"$LOG"
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

wikidata_plan_qid_count() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
before = path.lstat()
if path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
    raise SystemExit("request plan is not a single-link regular file")
if stat.S_IMODE(before.st_mode) != 0o644:
    raise SystemExit("request plan must have mode 0644")
data = path.read_bytes()
after = path.lstat()
if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
):
    raise SystemExit("request plan changed while being read")

def object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key {key!r}")
        value[key] = item
    return value

try:
    plan = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=object_without_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite number {value!r}")
        ),
    )
except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
    raise SystemExit(f"invalid request-plan JSON: {exc}") from exc
if not isinstance(plan, dict) or set(plan) != {"schema", "qids"}:
    raise SystemExit("request plan must contain exactly schema and qids")
if plan["schema"] != "wikilean.wikidata-entity-request-plan/v1":
    raise SystemExit("unexpected request-plan schema")
qids = plan["qids"]
if not isinstance(qids, list) or len(qids) > 50000:
    raise SystemExit("request-plan qids must be an array of at most 50000 entries")
qid_pattern = re.compile(r"Q[1-9][0-9]{0,11}")
if any(not isinstance(qid, str) or qid_pattern.fullmatch(qid) is None for qid in qids):
    raise SystemExit("request plan contains a non-canonical QID")
if qids != sorted(set(qids)):
    raise SystemExit("request-plan QIDs must be unique and sorted")
canonical = json.dumps(
    plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
).encode("utf-8")
if data != canonical:
    raise SystemExit("request plan is not canonical JSON")
print(len(qids))
PY
}

wikidata_bundle_path() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import re
import stat
import sys
from pathlib import Path

stdout_path, store = map(Path, sys.argv[1:])
raw = stdout_path.read_bytes()
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit("acquirer stdout is not UTF-8") from exc
if not text.endswith("\n") or text.count("\n") != 1:
    raise SystemExit("acquirer must print exactly one newline-terminated path")
line = text[:-1]
if not line or line.strip() != line:
    raise SystemExit("acquirer output path is empty or padded")
target = Path(line)
if not store.is_absolute() or not target.is_absolute():
    raise SystemExit("bundle store and acquired path must be absolute")
store_metadata = store.lstat()
target_metadata = target.lstat()
if store.is_symlink() or not stat.S_ISDIR(store_metadata.st_mode) \
        or stat.S_IMODE(store_metadata.st_mode) != 0o700:
    raise SystemExit("bundle store is not a real mode-0700 directory")
if target.is_symlink() or not stat.S_ISDIR(target_metadata.st_mode) \
        or stat.S_IMODE(target_metadata.st_mode) != 0o700:
    raise SystemExit("acquirer output is not a real mode-0700 directory")
store_real = store.resolve(strict=True)
target_real = target.resolve(strict=True)
try:
    relative = target_real.relative_to(store_real)
except ValueError as exc:
    raise SystemExit("acquirer output escaped the configured store") from exc
if len(relative.parts) != 1 or re.fullmatch(r"[0-9a-f]{64}", relative.name) is None:
    raise SystemExit("acquirer output is not a content-addressed store child")
print(target_real)
PY
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
  FOLD_ARGS=()
  WIKIDATA_REQUEST_PLAN="$RUN_DIR/wikidata-request-plan.json"
  WIKIDATA_ACQUIRE_STDOUT="$RUN_DIR/wikidata-acquire.stdout"
  WIKIDATA_QID_COUNT=""
  WIKIDATA_BUNDLE=""

  echo "=== plan sealed Wikidata evidence for proposal fold ==="
  if "$PYTHON_BIN" "$REPO/brain/fold_proposals.py" \
      --write-wikidata-request-plan "$WIKIDATA_REQUEST_PLAN"; then
    if WIKIDATA_QID_COUNT="$(wikidata_plan_qid_count "$WIKIDATA_REQUEST_PLAN")" \
        && case "$WIKIDATA_QID_COUNT" in ''|*[!0-9]*) false ;; *) true ;; esac; then
      echo "(Wikidata request plan GREEN: $WIKIDATA_QID_COUNT QIDs)"
    else
      echo "!!! Wikidata request plan validation FAILED — build and release aborted"
      PUBLISH_OK=0
    fi
  else
    echo "!!! Wikidata request planning FAILED — build and release aborted"
    PUBLISH_OK=0
  fi

  if [ "$PUBLISH_OK" = "1" ] && [ "$WIKIDATA_QID_COUNT" -gt 0 ]; then
    echo "=== acquire sealed Wikidata entity evidence ==="
    if WIKILEAN_PYTHON="$PYTHON_BIN" \
        "$REPO/brain/acquire-wikidata-entities.sh" \
        "$WIKIDATA_REQUEST_PLAN" --store "$WIKIDATA_ENTITY_STORE" \
        >"$WIKIDATA_ACQUIRE_STDOUT"; then
      if WIKIDATA_BUNDLE="$(wikidata_bundle_path \
          "$WIKIDATA_ACQUIRE_STDOUT" "$WIKIDATA_ENTITY_STORE")"; then
        FOLD_ARGS=(--wikidata-entity-bundle "$WIKIDATA_BUNDLE")
        echo "(Wikidata entity acquisition GREEN: $WIKIDATA_BUNDLE)"
      else
        echo "!!! Wikidata acquisition returned an invalid bundle path — build and release aborted"
        PUBLISH_OK=0
      fi
    else
      echo "!!! Wikidata entity acquisition FAILED — build and release aborted"
      PUBLISH_OK=0
    fi
  elif [ "$PUBLISH_OK" = "1" ]; then
    echo "(Wikidata request set empty — acquisition skipped)"
  fi

  echo "=== fold proposals (deterministic sealed-evidence verifier) ==="
  if [ "$PUBLISH_OK" = "1" ]; then
    if [ -n "$WIKIDATA_BUNDLE" ]; then
      "$PYTHON_BIN" "$REPO/brain/fold_proposals.py" "${FOLD_ARGS[@]}"
    else
      "$PYTHON_BIN" "$REPO/brain/fold_proposals.py"
    fi
    if [ "$?" = "0" ]; then
      echo "(fold GREEN)"
    else
      echo "!!! fold_proposals FAILED — build and publish aborted; see fold output above"
      PUBLISH_OK=0
    fi
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
  # The release commits its reducer and Worker/static source authority. Generated
  # site/out releases and ignored wiki/public bytes do not appear in this check.
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
      brain/acquire-wikidata-entities.sh brain/acquire_wikidata_entities.py \
      brain/wikidata_entity_bundle.py brain/fold_proposals.py brain/ingest/common.py \
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

  if [ "$PUBLISH_OK" = "1" ]; then
    echo "=== release: atomic shadow public staging ==="
    STAGE_ARGS=(
      --brain-release-manifest "$RELEASE_MANIFEST"
      --brain-release-dir "$RELEASE_ROOT"
    )
    TMP_PUBLIC_OUTPUT="$(mktemp "$LOGDIR/.brain-public-build.XXXXXX")"
    TMP_PUBLIC_RESULT="$(mktemp "$REPO/site/out/.brain-public-result.XXXXXX")"
    if (cd "$REPO/wiki" \
          && node --experimental-strip-types scripts/build-public.ts "${STAGE_ARGS[@]}" \
            >"$TMP_PUBLIC_OUTPUT") \
        && extract_json_object "$TMP_PUBLIC_OUTPUT" "$TMP_PUBLIC_RESULT" \
             schema public_dir mathlib_declarations public_baseline brain duration_ms max_rss_bytes \
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

  # ---- 5. SHADOW RESULT ------------------------------------------------------
  if [ "$PUBLISH_OK" = "1" ]; then
    echo "(shadow-only — verified release $RELEASE_ID is staged locally; production unchanged)"
  else
    echo "!!! PUBLISH ABORTED — see the build/release failure above; production unchanged"
    RUN_STATUS=1
  fi
  echo "=== done $(date +%Y%m%dT%H%M%S) ==="
} >>"$LOG" 2>&1

# Retain the last 30 run logs.
ls -1t "$LOGDIR"/brain-*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true
exit "$RUN_STATUS"
