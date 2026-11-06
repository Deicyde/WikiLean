#!/usr/bin/env bash
# The agent bridge a contributor machine runs: one `claude -p` call, sandboxed.
#
# docs/PIPELINE.md section 3 (stage 11), invariants 2 and 3; the settings themselves are
# sandbox.py, the one place that writes them.
#
# A run host confines its agents with its own Linux sandbox. A contributor machine is a
# Mac, so the confinement is Claude Code's own: a settings file passed to
# `claude --settings`, written PER CALL.
#
# Environment:
#   WIKILEAN_ROLE              worker|reviewer (default: $1, then "worker")
#   WIKILEAN_PROMPT_DIR        where <role>.md and the optional <role>-system.md live
#   WIKILEAN_MODEL             --model id; unset uses the CLI's default
#   WIKILEAN_WORKER_TOOLS      --allowedTools for the worker
#   WIKILEAN_REVIEWER_TOOLS    --allowedTools for the reviewer (a read-only surface)
#   WIKILEAN_CLAUDE_SETTINGS   the settings file (sandbox.SETTINGS_ENV). A file holding
#                              {{WIKILEAN_TASK_WORKTREE}} is a TEMPLATE: this script
#                              writes one settings file per call with that placeholder
#                              replaced by the directory the call was run in, which is
#                              the call's own task worktree -- the thing a file written
#                              at stage time cannot name
#   TEND_TASK_ID / TEND_WORKTREE_PATH / TEND_AGENT_SESSION_DIR / TEND_AGENT_RESUME
#                              the orchestrator's, substituted into the prompt
#
# bash 3.2: macOS ships it, so no heredoc inside $(...), no associative arrays, no
# `${var^^}`. `bash -n` over this file is one of the Mac checks (docs/PIPELINE.md 9.5).
#
# Exit 78 means "I could not confine this call": the settings file named is unreadable,
# or this call's own copy could not be written. Running the agent anyway would run it
# unsandboxed, which is the one thing this script exists to stop.
set -euo pipefail

ROLE="${WIKILEAN_ROLE:-worker}"
RESUME=0
for arg in "$@"; do
  case "$arg" in
    worker|reviewer) ROLE="$arg" ;;
    --resume) RESUME=1 ;;
  esac
done
[ "${TEND_AGENT_RESUME:-0}" = "1" ] && RESUME=1

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROMPTS="${WIKILEAN_PROMPT_DIR:-$SCRIPT_DIR/prompts}"
SESSION_DIR="${TEND_AGENT_SESSION_DIR:-$PWD/.tend}"
mkdir -p "$SESSION_DIR"
SESSION_ID_FILE="$SESSION_DIR/claude-session-id"

# THE SANDBOX, settled before anything else is read.
SETTINGS="${WIKILEAN_CLAUDE_SETTINGS:-}"
if [ -n "$SETTINGS" ] && [ ! -r "$SETTINGS" ]; then
  echo "bridge: WIKILEAN_CLAUDE_SETTINGS=$SETTINGS is not readable; refusing to run the agent unsandboxed" >&2
  exit 78
fi
# PER CALL: a settings file whose write root is THIS call's worktree, which is the
# directory the orchestrator runs the wrapper in. It is written inside that worktree's
# own .tend/, which the entrypoint's git excludes, so it is no part of any change set.
# A settings file without the placeholder -- an operator's own -- is passed through.
if [ -n "$SETTINGS" ] && grep -qF '{{WIKILEAN_TASK_WORKTREE}}' "$SETTINGS"; then
  # the PHYSICAL path (`pwd -P`): the sandbox matches the real path a write lands on,
  # and on macOS a worktree under /var or /tmp is reached through a symlink into /private
  CALL_DIR=$(pwd -P)
  CALL_SETTINGS="$CALL_DIR/.tend/claude-settings.json"
  if ! mkdir -p "$CALL_DIR/.tend" || ! sed -e "s#{{WIKILEAN_TASK_WORKTREE}}#$CALL_DIR#g" "$SETTINGS" >"$CALL_SETTINGS"; then
    echo "bridge: could not write this call's sandbox settings to $CALL_SETTINGS; refusing to run the agent unsandboxed" >&2
    exit 78
  fi
  SETTINGS="$CALL_SETTINGS"
fi

if [ ! -r "$PROMPTS/$ROLE.md" ]; then
  echo "bridge: no prompt at $PROMPTS/$ROLE.md" >&2
  exit 78
fi
PROMPT="$(sed -e "s#\$TEND_TASK_ID#${TEND_TASK_ID:-unknown}#g" \
              -e "s#\$TEND_WORKTREE_PATH#${TEND_WORKTREE_PATH:-$PWD}#g" "$PROMPTS/$ROLE.md")"

CLAUDE_ARGS=(--output-format json)
if [ -n "$SETTINGS" ]; then
  CLAUDE_ARGS+=(--settings "$SETTINGS")
fi
if [ "$ROLE" = reviewer ]; then
  # The reviewer judges the branch; it does not edit it.
  REVIEWER_DEFAULT="Read,Glob,Grep,Bash(git diff:*),Bash(git show:*),Bash(git log:*),Bash(git status:*)"
  CLAUDE_ARGS+=(--allowedTools "${WIKILEAN_REVIEWER_TOOLS:-$REVIEWER_DEFAULT}")
else
  CLAUDE_ARGS+=(--permission-mode acceptEdits
                --allowedTools "${WIKILEAN_WORKER_TOOLS:-Read,Glob,Grep,Edit,Write,Bash(git:*)}")
fi
[ -n "${WIKILEAN_MODEL:-}" ] && CLAUDE_ARGS+=(--model "$WIKILEAN_MODEL")
if [ -r "$PROMPTS/$ROLE-system.md" ]; then
  CLAUDE_ARGS+=(--append-system-prompt "$(cat "$PROMPTS/$ROLE-system.md")")
fi
if [ "$RESUME" = 1 ] && [ -s "$SESSION_ID_FILE" ]; then
  CLAUDE_ARGS+=(--resume "$(cat "$SESSION_ID_FILE")")
fi

# AGENTS.md, "Max-auth gotcha": an ANTHROPIC_API_KEY in the environment makes a
# subscription call fail silently ("error result: success", 0 tokens).
unset ANTHROPIC_API_KEY

exec claude "${CLAUDE_ARGS[@]}" -p "$PROMPT"
