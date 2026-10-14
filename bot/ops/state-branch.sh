#!/usr/bin/env bash
# Keep the poller's mutable state on an unprotected branch instead of main.
#
# main requires the 'required' status check, so the poller can no longer push
# its '[skip ci]' state commit there. The state lives on BOT_STATE_BRANCH
# (default: bot-state), an orphan branch that holds only STATE_PATHS. main keeps
# its copy of bot/state/ as a frozen snapshot (the Brain build reads it).
#
#   state-branch.sh restore   make the working tree's STATE_PATHS match the branch
#   state-branch.sh save MSG  commit the working tree's STATE_PATHS onto the
#                             restored commit and push it (fast-forward only)
#
# restore records the commit it read; save refuses to run without that record.
# The branch is created once: with BOT_STATE_BOOTSTRAP=true (the workflow's
# bootstrap_state dispatch input), restore records 'absent' for a missing branch
# and the next save bootstraps it from the working tree, which is main's
# bot/state/. Without the flag a missing branch fails the run: after the first
# save it means the branch was deleted or renamed, and seeding from main's
# frozen snapshot again is the clobber this script exists to prevent; for the
# same reason a re-run attempt never bootstraps. A push that is not a
# fast-forward of the restored commit is rejected, so a concurrent writer fails
# the run instead of being overwritten. Any other read error fails rather than
# fall back to main's snapshot: acting on a stale copy of the state is how
# batch 7 was clobbered in 2026-08.
set -euo pipefail

BRANCH="${BOT_STATE_BRANCH:-bot-state}"
REMOTE="${BOT_STATE_REMOTE:-origin}"
# bot/state/ plus the bot/data/ files the tick writes and later reads: the harvested
# corrections, and the tagged-in-master lists open_batch refreshes before each open
# (settle-time pool exclusion and brain_queue read them). main's copies of those
# lists advance only through a fold-back PR or a manual refresh_tagged.py run.
STATE_PATHS=(bot/state bot/data/corrections.jsonl bot/data/tagged_in_master.txt bot/data/tagged_pairs.txt)

die() { echo "state-branch: $*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"
STATE_GIT_DIR="$(git rev-parse --absolute-git-dir)"
BASE_FILE="$STATE_GIT_DIR/wikilean-bot-state-base"

in_state_paths() {
  local path="$1" allowed
  for allowed in "${STATE_PATHS[@]}"; do
    [ "$path" = "$allowed" ] && return 0
    case "$path" in "$allowed"/*) return 0 ;; esac
  done
  return 1
}

restore() {
  rm -f "$BASE_FILE"
  local rc=0
  git ls-remote --exit-code --heads "$REMOTE" "refs/heads/$BRANCH" >/dev/null || rc=$?
  if [ "$rc" -eq 2 ]; then
    [ "${BOT_STATE_BOOTSTRAP:-}" = true ] || die "$REMOTE/$BRANCH does not exist." \
      "If it ever existed, it was deleted or renamed: do NOT bootstrap, recreate it at" \
      "its last commit (bot/README.md, 'Restore failed'). Only on the first run after" \
      "the state moved off main, dispatch the workflow with bootstrap_state=true."
    # A re-run reuses the original dispatch's inputs, so re-running an old
    # bootstrap run after the branch is deleted would seed from main again.
    [ "${GITHUB_RUN_ATTEMPT:-1}" = 1 ] || die "refusing to bootstrap $REMOTE/$BRANCH on" \
      "re-run attempt $GITHUB_RUN_ATTEMPT, which reuses an old dispatch's bootstrap_state." \
      "Dispatch a new run instead (bot/README.md, 'Restore failed')."
    echo "state-branch: $REMOTE/$BRANCH does not exist; bootstrap_state is set, so the next save creates it from this checkout"
    echo absent >"$BASE_FILE"
    return
  fi
  [ "$rc" -eq 0 ] || die "cannot read $REMOTE/$BRANCH (git ls-remote exit $rc)"
  git fetch --quiet --no-tags "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
  local base path paths=()
  base="$(git rev-parse --verify "refs/remotes/$REMOTE/$BRANCH^{commit}")"
  while IFS= read -r -d '' path; do
    in_state_paths "$path" || die "$BRANCH:$path is outside the state paths"
  done < <(git ls-tree -r -z --name-only "$base")
  # Without it a tick would read main's frozen current_pr: the stale-state clobber.
  git cat-file -e "$base:bot/state/bot_state.json" 2>/dev/null \
    || die "$BRANCH ($base) has no bot/state/bot_state.json"
  # Mirror every state path the branch carries, deletions included (--no-overlay
  # removes tracked files the branch lacks). A path the branch has never carried
  # keeps main's copy, and the next save adds it.
  for path in "${STATE_PATHS[@]}"; do
    if git cat-file -e "$base:$path" 2>/dev/null; then
      paths+=("$path")
    fi
  done
  git checkout --no-overlay "$base" -- "${paths[@]}"
  echo "$base" >"$BASE_FILE"
  echo "state-branch: restored ${paths[*]} from $REMOTE/$BRANCH at $base"
}

save() {
  local message="${1:?usage: state-branch.sh save MESSAGE}"
  [ -f "$BASE_FILE" ] || die "no restore record; run 'state-branch.sh restore' first"
  local base
  base="$(cat "$BASE_FILE")"
  # A private index: main's index and working tree stay as they are.
  export GIT_INDEX_FILE="$STATE_GIT_DIR/wikilean-bot-state-index"
  rm -f "$GIT_INDEX_FILE"
  if [ "$base" = absent ]; then
    git read-tree --empty
  else
    git read-tree "$base"
  fi
  local path add=()
  for path in "${STATE_PATHS[@]}"; do
    # Stage present paths, and removals of paths the branch already tracks.
    if [ -e "$path" ] || [ -n "$(git ls-files -- "$path")" ]; then
      add+=("$path")
    fi
  done
  if [ "${#add[@]}" -gt 0 ]; then
    git add -- "${add[@]}"
  fi
  local tree
  tree="$(git write-tree)"
  while IFS= read -r -d '' path; do
    in_state_paths "$path" || die "refusing to save $path: outside the state paths"
  done < <(git ls-tree -r -z --name-only "$tree")
  rm -f "$GIT_INDEX_FILE"
  unset GIT_INDEX_FILE
  local commit
  if [ "$base" = absent ]; then
    commit="$(git commit-tree "$tree" -m "$message")"
  elif [ "$tree" = "$(git rev-parse "$base^{tree}")" ]; then
    echo "no state change"
    return
  else
    commit="$(git commit-tree "$tree" -p "$base" -m "$message")"
  fi
  git push --quiet "$REMOTE" "$commit:refs/heads/$BRANCH"
  echo "state-branch: pushed $commit to $REMOTE/$BRANCH"
}

case "${1:-}" in
  restore) restore ;;
  save) shift; save "$@" ;;
  *) die "usage: state-branch.sh restore | save MESSAGE" ;;
esac
