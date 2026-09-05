#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
REPO="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"

if [ -n "${WIKILEAN_PYTHON:-}" ]; then
  PYTHON_BIN="$WIKILEAN_PYTHON"
elif [ -x "$REPO/.venv/bin/python3" ]; then
  PYTHON_BIN="$REPO/.venv/bin/python3"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.12)"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [ -z "${PYTHON_BIN:-}" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "FATAL: CPython 3.12 is required for Wikidata entity acquisition" >&2
  exit 2
fi
if ! "$PYTHON_BIN" -I -S -c \
    'import platform, sys; raise SystemExit(0 if platform.python_implementation() == "CPython" and sys.version_info[:2] == (3, 12) else 1)'; then
  echo "FATAL: CPython 3.12 is required for Wikidata entity acquisition ($PYTHON_BIN is incompatible)" >&2
  exit 2
fi

exec "$PYTHON_BIN" -I -S "$SCRIPT_DIR/acquire_wikidata_entities.py" "$@"
