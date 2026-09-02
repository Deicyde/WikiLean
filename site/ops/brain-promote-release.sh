#!/bin/bash
# Promote one already-frozen Brain release. This wrapper deliberately performs
# no ingest, reduction, or deployment logic; the Python state machine owns the
# exact-release checks and durable journal.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
REPO="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

for required in \
  "$REPO/brain/tools/verify_release.py" \
  "$REPO/site/ops/brain_public_baseline.py" \
  "$REPO/site/ops/brain_promote_release.py" \
  "$REPO/wiki/package.json" \
  "$REPO/wiki/wrangler.jsonc"; do
  if [ ! -f "$required" ]; then
    printf 'brain-promote-release: invalid repository root %s (missing %s)\n' \
      "$REPO" "$required" >&2
    exit 1
  fi
done

[ -f "$SCRIPT_DIR/nightly.env" ] && . "$SCRIPT_DIR/nightly.env"
[ -f "$SCRIPT_DIR/nightly.local.env" ] && . "$SCRIPT_DIR/nightly.local.env"

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

if [ ! -x "$PYTHON_BIN" ] \
    || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  printf 'brain-promote-release: Python 3.12+ is required (selected %s)\n' \
    "${PYTHON_BIN:-none}" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/brain_promote_release.py" \
  "$@" \
  --repo-root "$REPO" \
  --python "$PYTHON_BIN"
