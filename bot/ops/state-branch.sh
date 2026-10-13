#!/usr/bin/env bash
# Keep the poller's mutable state on an unprotected branch instead of main.
#
# main requires the 'required' status check, so the poller can no longer push
# its '[skip ci]' state commit there. The state lives on BOT_STATE_BRANCH
# (default: bot-state), an orphan branch that holds only STATE_PATHS. main keeps
# its copy of bot/state/ as a frozen snapshot (the Brain build reads it).
#
#   state-branch.sh restore   overlay the branch's files onto the working tree
#   state-branch.sh save MSG  commit the working tree's STATE_PATHS onto the
#                             restored commit and push it (fast-forward only)
#
# restore records the commit it read, or 'absent' when the branch does not exist
# yet; save refuses to run without that record. A first save on an absent branch
# bootstraps it from the working tree, which is main's bot/state/. A push that
# is not a fast-forward of the restored commit is rejected, so a concurrent
# writer fails the run instead of being overwritten. Any read error other than
# "branch absent" fails rather than fall back to main's snapshot: acting on a
# stale copy of the state is how batch 7 was clobbered in 2026-08.
set -euo pipefail

BRANCH="${BOT_STATE_BRANCH:-bot-state}"
REMOTE="${BOT_STATE_REMOTE:-origin}"
# bot/state/ plus the one bot-written dataset in bot/data/ that accumulates. The
# other bot/data files the tick writes are caches it regenerates (tagged_in_master
# is refreshed before every open), and main's copies stay authoritative for them.
STATE_PATHS=(bot/state bot/data/corrections.jsonl)

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
    echo "state-branch: $REMOTE/$BRANCH does not exist yet; the next save bootstraps it from this checkout"
    echo absent >"$BASE_FILE"
    return
  fi
  [ "$rc" -eq 0 ] || die "cannot read $REMOTE/$BRANCH (git ls-remote exit $rc)"
  git fetch --quiet --no-tags "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
  local base files=()
  base="$(git rev-parse --verify "refs/remotes/$REMOTE/$BRANCH^{commit}")"
  while IFS= read -r -d '' path; do
    in_state_paths "$path" || die "$BRANCH:$path is outside the state paths"
    files+=("$path")
  done < <(git ls-tree -r -z --name-only "$base")
  [ "${#files[@]}" -gt 0 ] || die "$BRANCH ($base) holds no state files"
  git checkout "$base" -- "${files[@]}"
  echo "$base" >"$BASE_FILE"
  echo "state-branch: restored ${#files[@]} files from $REMOTE/$BRANCH at $base"
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
