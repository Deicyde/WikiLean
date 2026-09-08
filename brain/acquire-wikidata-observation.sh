#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
REPO="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
if [ -n "${WIKILEAN_PYTHON:-}" ]; then
  PYTHON_BIN="$WIKILEAN_PYTHON"
elif [ -x "$REPO/.venv/bin/python3" ]; then
  PYTHON_BIN="$REPO/.venv/bin/python3"
else
  PYTHON_BIN="$(command -v python3.12 || true)"
fi
if [ -z "${PYTHON_BIN:-}" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "FATAL: CPython 3.12 is required for sealed Wikidata observation" >&2
  exit 2
fi
exec "$PYTHON_BIN" -I -S "$SCRIPT_DIR/acquire_wikidata_observation.py" "$@"
