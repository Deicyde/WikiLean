#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

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
  echo "FATAL: Python 3.12 is required for the annotation mirror" >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "FATAL: Python 3.12 is required for the annotation mirror ($PYTHON_BIN is incompatible)" >&2
  exit 2
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/pull_annotations.py" "$@"
