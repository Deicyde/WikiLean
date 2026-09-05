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
run_check "Hugging Face acquisition tests" "$PYTHON_BIN" catalog/test_huggingface_download.py
run_check "D1 acquisition snapshot tests" "$PYTHON_BIN" brain/test_acquire_d1_snapshot.py
run_check "Brain fold finalization tests" "$PYTHON_BIN" brain/test_fold_proposals.py
run_check "Brain authority contract tests" "$PYTHON_BIN" brain/test_authority_contracts.py
run_check "Brain offline-pack compiler tests" "$PYTHON_BIN" brain/test_compile_offline_pack_v2.py
run_check "Brain offline-pack preflight tests" "$PYTHON_BIN" brain/test_preflight_offline_pack_v2.py
run_check "Brain execution environment tests" "$PYTHON_BIN" brain/test_execution_environment.py
run_check "Brain build context tests" "$PYTHON_BIN" brain/test_build_context.py
run_check "Brain replay preparation tests" "$PYTHON_BIN" brain/test_prepare_replay_v2.py
run_check "Brain base graph context tests" "$PYTHON_BIN" brain/test_base_graph_context.py
run_check "Brain top-level shard publication tests" "$PYTHON_BIN" brain/test_build_shards.py
run_check "Brain cells context tests" "$PYTHON_BIN" brain/test_build_cells_context.py
run_check "Brain frontier context tests" "$PYTHON_BIN" brain/test_build_frontier_context.py
run_check "Brain cell shard context tests" "$PYTHON_BIN" brain/test_build_cell_shards_context.py
run_check "Brain full-DAG replay tests" "$PYTHON_BIN" brain/test_run_replay_v2.py
run_check "Brain replay sandbox kernel test" "$PYTHON_BIN" -I brain/test_replay_sandbox.py
run_check "Brain release builder tests" "$PYTHON_BIN" brain/test_release_builder.py
run_check "Brain store metrics tests" "$PYTHON_BIN" brain/test_store_metrics.py
run_check "Brain semantic diff tests" "$PYTHON_BIN" brain/test_semantic_diff.py
run_check "Brain trusted transport tests" "$PYTHON_BIN" site/ops/test_brain_http.py
run_check "Brain release canary tests" "$PYTHON_BIN" site/ops/test_brain_canary.py
run_check "Brain deployment journal tests" "$PYTHON_BIN" site/ops/test_brain_deploy_journal.py
run_check "Brain public asset baseline tests" "$PYTHON_BIN" site/ops/test_brain_public_baseline.py
run_check "Brain exact-release promoter tests" "$PYTHON_BIN" site/ops/test_brain_promote_release.py
run_check "Brain activation CI evidence tests" "$PYTHON_BIN" site/ops/test_brain_activation_ci.py
run_check "Brain activation evidence bundle tests" "$PYTHON_BIN" site/ops/test_brain_activation_bundle.py
run_check "Brain activation evidence integration test" "$PYTHON_BIN" site/ops/test_brain_activation_bundle_integration.py
run_check "Brain nightly shell tests" "$PYTHON_BIN" site/ops/test_brain_nightly.py
run_check "Frontier suitability policy tests" "$PYTHON_BIN" brain/test_frontier_suitability.py
run_check "Frontier generated-page contract" "$PYTHON_BIN" site/test_frontier_page.py

printf '\nCI Python summary: 36 commands passed; all required offline scenarios ran.\n'
