#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

cd "$ROOT"
export PYTHONHASHSEED=0
export PYTHONDONTWRITEBYTECODE=1
export TZ=UTC

# CI must never inherit credentials that could turn an offline test into a live call.
unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN PIPELINE_TOKEN CLOUDFLARE_API_TOKEN

run_check() {
  local name="$1"
  shift
  printf '\n==> %s\n' "$name"
  printf '    '
  printf '%q ' "$@"
  printf '\n'
  "$@"
}

"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), f"Python 3.12 required, found {sys.version.split()[0]}"'

run_check "moderation unit tests" "$PYTHON_BIN" site/test_moderate.py
run_check "cross-language parity tests" "$PYTHON_BIN" site/test_parity.py
run_check "offline moderation evaluation" "$PYTHON_BIN" site/eval_moderation.py --offline --require-all
run_check "Brain v2 fixture tests" "$PYTHON_BIN" brain/test_v2.py
run_check "Brain storage fixture tests" "$PYTHON_BIN" brain/test_store.py
run_check "Brain harvest fixture tests" "$PYTHON_BIN" brain/test_harvest.py
run_check "Brain fold finalization tests" "$PYTHON_BIN" brain/test_fold_proposals.py
run_check "Brain authority contract tests" "$PYTHON_BIN" brain/test_authority_contracts.py
run_check "Brain semantic diff tests" "$PYTHON_BIN" brain/test_semantic_diff.py
run_check "Frontier suitability policy tests" "$PYTHON_BIN" brain/test_frontier_suitability.py
run_check "Frontier generated-page contract" "$PYTHON_BIN" site/test_frontier_page.py

printf '\nCI Python summary: 11 commands passed; all required offline scenarios ran.\n'
