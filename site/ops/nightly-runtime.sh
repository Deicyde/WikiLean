#!/bin/bash
# Shared runtime preflight for the moderation and new-article launchers.
#
# This file may be sourced by a launcher or executed as
# `nightly-runtime.sh check`.  It deliberately performs no WikiLean job work.

wikilean_ops_error() {
  printf 'wikilean nightly preflight: %s\n' "$*" >&2
}

wikilean_ops_python_312() {
  [ -n "${1:-}" ] && [ -x "$1" ] \
    && "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
      >/dev/null 2>&1
}

wikilean_ops_init() {
  local mode runtime_dir required candidate configured_python token_file
  local inherited_python inherited_mathlib inherited_brain_mathlib python_dir

  mode="${1:-moderation}"
  case "$mode" in
    moderation|brain|all) ;;
    *) wikilean_ops_error "unknown preflight mode: $mode"; return 2 ;;
  esac

  # Values explicitly carried by a rendered plist (or a deliberate manual
  # invocation) take precedence over both tracked defaults and local config.
  inherited_python="${WIKILEAN_PYTHON:-}"
  inherited_mathlib="${WIKILEAN_MATHLIB:-}"
  inherited_brain_mathlib="${BRAIN_MATHLIB_CHECKOUT:-}"

  runtime_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" \
    || { wikilean_ops_error "cannot resolve site/ops"; return 1; }
  REPO="$(CDPATH= cd -- "$runtime_dir/../.." && pwd -P)" \
    || { wikilean_ops_error "cannot resolve repository root"; return 1; }
  export REPO

  for required in \
    "$REPO/site/moderate.py" \
    "$REPO/site/ops/nightly.env" \
    "$REPO/wiki/package.json"; do
    if [ ! -f "$required" ]; then
      wikilean_ops_error "derived repository root $REPO is invalid (missing $required)"
      return 1
    fi
  done

  export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

  # Tracked defaults first, then the gitignored host-local paths.  REPO is set
  # before either file is sourced so local config can remain checkout-relative.
  # shellcheck disable=SC1090
  . "$REPO/site/ops/nightly.env"
  if [ -f "$REPO/site/ops/nightly.local.env" ]; then
    # shellcheck disable=SC1090
    . "$REPO/site/ops/nightly.local.env"
  fi
  [ -n "$inherited_python" ] && WIKILEAN_PYTHON="$inherited_python"
  [ -n "$inherited_mathlib" ] && WIKILEAN_MATHLIB="$inherited_mathlib"
  [ -n "$inherited_brain_mathlib" ] && BRAIN_MATHLIB_CHECKOUT="$inherited_brain_mathlib"

  configured_python="${WIKILEAN_PYTHON:-}"
  if [ -n "$configured_python" ]; then
    case "$configured_python" in
      /*) ;;
      *)
        wikilean_ops_error "WIKILEAN_PYTHON must be an absolute Python 3.12+ path (configured: $configured_python)"
        return 1
        ;;
    esac
    if ! wikilean_ops_python_312 "$configured_python"; then
      wikilean_ops_error "WIKILEAN_PYTHON must be an executable Python 3.12+ path (configured: $configured_python)"
      return 1
    fi
    python_dir="$(CDPATH= cd -- "$(dirname -- "$configured_python")" && pwd -P)" \
      || { wikilean_ops_error "cannot resolve WIKILEAN_PYTHON=$configured_python"; return 1; }
    PY="$python_dir/$(basename -- "$configured_python")"
  else
    PY=""
    for candidate in \
      "$REPO/.venv/bin/python3" \
      "$REPO/catalog/.venv/bin/python3" \
      /usr/local/bin/python3 \
      /opt/homebrew/bin/python3; do
      if wikilean_ops_python_312 "$candidate"; then
        PY="$candidate"
        break
      fi
    done
    if [ -z "$PY" ]; then
      candidate="$(command -v python3 2>/dev/null || true)"
      if wikilean_ops_python_312 "$candidate"; then
        PY="$candidate"
      fi
    fi
    if [ -z "$PY" ]; then
      wikilean_ops_error "Python 3.12+ was not found; set WIKILEAN_PYTHON in $REPO/site/ops/nightly.local.env"
      return 1
    fi
  fi
  WIKILEAN_PYTHON="$PY"
  export PY WIKILEAN_PYTHON

  if [ "$mode" = "moderation" ] || [ "$mode" = "all" ]; then
    if [ -z "${WIKILEAN_MATHLIB:-}" ]; then
      wikilean_ops_error "WIKILEAN_MATHLIB is required; set the mathlib4 checkout in $REPO/site/ops/nightly.local.env"
      return 1
    fi
    case "$WIKILEAN_MATHLIB" in
      /*) ;;
      *)
        wikilean_ops_error "WIKILEAN_MATHLIB must be absolute (configured: $WIKILEAN_MATHLIB)"
        return 1
        ;;
    esac
    if [ ! -d "$WIKILEAN_MATHLIB/Mathlib" ] || [ ! -r "$WIKILEAN_MATHLIB/Mathlib" ]; then
      wikilean_ops_error "WIKILEAN_MATHLIB must name a readable mathlib4 checkout containing Mathlib/ (configured: $WIKILEAN_MATHLIB)"
      return 1
    fi
    WIKILEAN_MATHLIB="$(CDPATH= cd -- "$WIKILEAN_MATHLIB" && pwd -P)" \
      || { wikilean_ops_error "cannot resolve WIKILEAN_MATHLIB=$WIKILEAN_MATHLIB"; return 1; }
    export WIKILEAN_MATHLIB

    if [ -z "${WIKILEAN_API_TOKEN:-}" ]; then
      token_file="$REPO/wiki/.dev.vars"
      if [ -r "$token_file" ]; then
        WIKILEAN_API_TOKEN="$(sed -n 's/^PIPELINE_TOKEN=//p' "$token_file" | sed -n '1p')"
      fi
    fi
    if [ -z "${WIKILEAN_API_TOKEN:-}" ]; then
      wikilean_ops_error "WIKILEAN_API_TOKEN is unavailable; set PIPELINE_TOKEN in $REPO/wiki/.dev.vars"
      return 1
    fi
    export WIKILEAN_API_TOKEN
  fi

  if [ "$mode" = "brain" ] || [ "$mode" = "all" ]; then
    if [ -z "${BRAIN_MATHLIB_CHECKOUT:-}" ]; then
      wikilean_ops_error "BRAIN_MATHLIB_CHECKOUT is required; set the mathlib4/Mathlib directory in $REPO/site/ops/nightly.local.env"
      return 1
    fi
    case "$BRAIN_MATHLIB_CHECKOUT" in
      /*) ;;
      *)
        wikilean_ops_error "BRAIN_MATHLIB_CHECKOUT must be absolute (configured: $BRAIN_MATHLIB_CHECKOUT)"
        return 1
        ;;
    esac
    if [ ! -d "$BRAIN_MATHLIB_CHECKOUT/Algebra" ] || [ ! -r "$BRAIN_MATHLIB_CHECKOUT/Algebra" ]; then
      wikilean_ops_error "BRAIN_MATHLIB_CHECKOUT must name a readable mathlib4/Mathlib directory containing Algebra/ (configured: $BRAIN_MATHLIB_CHECKOUT)"
      return 1
    fi
    BRAIN_MATHLIB_CHECKOUT="$(CDPATH= cd -- "$BRAIN_MATHLIB_CHECKOUT" && pwd -P)" \
      || { wikilean_ops_error "cannot resolve BRAIN_MATHLIB_CHECKOUT=$BRAIN_MATHLIB_CHECKOUT"; return 1; }
    export BRAIN_MATHLIB_CHECKOUT
  fi

  return 0
}

wikilean_ops_print_check() {
  printf 'wikilean nightly preflight ok\n'
  printf 'repo=%s\n' "$REPO"
  printf 'python=%s\n' "$PY"
  [ -n "${WIKILEAN_MATHLIB:-}" ] && printf 'mathlib=%s\n' "$WIKILEAN_MATHLIB"
  [ -n "${BRAIN_MATHLIB_CHECKOUT:-}" ] \
    && printf 'brain_mathlib=%s\n' "$BRAIN_MATHLIB_CHECKOUT"
  return 0
}

wikilean_ops_print_json() {
  "$PY" -I - "$REPO" "$PY" "${WIKILEAN_MATHLIB:-}" "${BRAIN_MATHLIB_CHECKOUT:-}" <<'PY'
import json
import sys

print(json.dumps({
    "repo": sys.argv[1],
    "python": sys.argv[2],
    "mathlib": sys.argv[3] or None,
    "brain_mathlib": sys.argv[4] or None,
}, sort_keys=True, separators=(",", ":")))
PY
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    check)
      wikilean_ops_init "${2:-moderation}" || exit 1
      wikilean_ops_print_check
      ;;
    json)
      wikilean_ops_init "${2:-moderation}" || exit 1
      wikilean_ops_print_json
      ;;
    *)
      wikilean_ops_error "usage: $0 {check|json} [moderation|brain|all]"
      exit 2
      ;;
  esac
fi
