# WikiLean daily `@[wikidata]` batch bot

Runs one batch of 10 `@[wikidata]` cross-reference tags at a time through review →
merge-or-recycle against `leanprover-community/mathlib4`. The poller
(`.github/workflows/wikidata-poll.yml` → `poll.py`) is event-driven: it opens the
next batch only after the current one merges.

## The loop

Each run does two phases:

1. **Settle** the current PR (`settle.py`, deterministic):
   - **Gate:** ≥2 distinct people with a recorded verdict on the PR, **counting the
     PR author**, at least one of them a "maintainer": a Mathlib maintainer listed
     in `settle.py` (`MAINTAINERS`), or anyone GitHub lists as an owner, member or
     collaborator of mathlib4 (which includes Mathlib reviewers). No minimum PR age
     is checked.
   - When the gate is open, classify each tag and **split** the PR down to its
     green tags (`split.py`, force-push), then post the tag table under a header
     stating the rule applied and "Ready for a maintainer's review" (`settle.ready_header`).
   - Any Mathlib maintainer merges the now-all-green PR (`bors merge`, or delegates
     with `bors d+`); reviewers route PRs to maintainers with a `maintainer merge`
     comment. The bot can't merge upstream.
2. **Open** the next batch (`open_batch_pr.py`): requeued (retargeted) recycled
   tags + Brain-suggested `formalizes` edges + fresh pool tags → 10 → new PR +
   crossref comments + `LLM-generated` label + side-by-side table.

## Per-tag rule (`settle.py`)

- A bare **note/comment = "defer to the other reviewer"** → ignored.
- A **maintainer's explicit approve/reject/revise/flag trumps** everything.
- Otherwise: any **reject/revise/flag → recycle**; else **≥1 approve → green**;
  else (no review) → recycle.

So one approval suffices per tag (silence = agreement), but any unresolved
objection recycles, and a maintainer overrides. Recording model: the detailed
reviewer GitHub-**Approves** the PR (blanket = approve all) + inline
revise/reject exceptions; the double-checking maintainer inline-flags only their
concerns.

## Recycle triage (`triage.py`) — the ONE LLM step

The deterministic settler produces the recycle set; `triage.py` asks an LLM (via
`claude -p`) to read each tag's reviewer notes + context and decide **requeue**
(a fixable retarget — proposes & verifies a `suggested_decl`) or **cut** (not
cleanly in Mathlib / not worth a review slot). Tag *generation* stays
deterministic — the LLM only proposes the target declaration; `open_batch_pr.py`
applies it.

## Determinism boundary (hard rule)

Everything is deterministic **except** `triage.py`. The split, the gate, the
green/recycle classification, PR opening, comments, and label are all plain
fetch/parse/git/gh — no LLM.

## Safety / rollout

- **Everything is `--dry-run` by default.** `daily_bot.py` and `split.py` mutate
  GitHub/Mathlib only with `--apply`.
- Run `--apply` **by hand** for the first several daily cycles and watch each
  step (force-push, daily PR, triage) before wrapping in cron.
- `split.py` force-pushes the *fork* branch with `--force-with-lease`.
- Agree the batch cadence with Mathlib maintainers before speeding it up.

## Run

    python3 daily_bot.py --mathlib ~/path/to/mathlib4              # dry-run
    python3 daily_bot.py --mathlib ~/path/to/mathlib4 --apply      # act

The live copy of `state/` is on the `bot-state` branch (see below).
`state/bot_state.json` tracks `{current_pr, batch_num, branch}`;
`state/recycle_queue.json` carries requeued retargets to the next batch.
`state/brain_queue.json` carries graduated Brain `formalizes` edges for the
review-gated Brain lane:

    python3 brain/harvest_community_edges.py   # graduate live Brain edits
    python3 bot/brain_queue.py                 # refresh state/brain_queue.json

## Files

| file | role | LLM? |
|---|---|---|
| `settle.py` | gate + green/recycle classification | no |
| `split.py` | remove recycled tags, rebuild, force-push to greens | no |
| `triage.py` | requeue-vs-cut + retarget suggestion | **yes** |
| `brain_queue.py` | graduated Brain formalizes edges → queue suggestions | no |
| `daily_bot.py` | orchestrator (settle → split → triage → open) | only via triage |
| `open_batch_pr.py` | apply tags + build + open PR (from the existing pipeline) | no |
| `ops/state-branch.sh` | restore/save the poller's state on the `bot-state` branch | no |

## State: the `bot-state` branch

`main` requires the strict `required` check, so the poller cannot push its state
commit there. Its mutable state (`bot/state/`, plus the `bot/data/` files the tick
writes and reads back: `corrections.jsonl`, `tagged_in_master.txt`,
`tagged_pairs.txt`) lives on the orphan `bot-state` branch instead, which branch
protection does not cover:

- Each run starts with `bot/ops/state-branch.sh restore`, which makes those paths
  in the checkout match the branch (deletions included) before anything reads
  them, and ends with
  `state-branch.sh save`, which commits any change on top of the restored commit
  and pushes it as a fast-forward. A failed read fails the run; it never falls back
  to `main`'s copy.
- The branch is created once, by hand: after the change that moved the state off
  `main` merges, run
  `gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean -f bootstrap_state=true`.
  That run seeds `bot-state` from `main`'s copies of the state paths. It does not
  tick or touch the upstream PR, but the next run does, and a scheduled run can
  start right after it. So check the seed before any run has a working token:
  compare `bot_state.json` (`current_pr`, `batch_num`, `branch`) and the open
  batch's `batchN_approved.json` with the live upstream PR, and fix them on
  `bot-state`. Without the flag a missing branch fails the run, because after the
  first save it means the branch was deleted or renamed, and seeding again from
  `main`'s stale snapshot is the clobber the branch exists to prevent. Once the
  branch exists, restore ignores the flag (the run still skips the tick), and a
  re-run attempt never bootstraps. See [Restore failed](#restore-failed) for
  recovery.
- `main`'s copies are now a frozen snapshot. Edits to them never reach the bot:
  restore overwrites them with the branch's copies and deletes `bot/state/` files
  the branch lacks. Everything below reads `main`'s copies, so it sees the bot's
  changes only after someone folds them back with a reviewed PR:
  - the Brain build: `bot/state/{cut_log,seed_queue,recycle_queue,brain_queue}.json`,
    `curated_git_input` entries in `brain/authority/reducer-inputs-v1.json`, read
    by `brain/build_cells.py` (the fifth entry, `pool_candidates.json`, is
    gitignored);
  - `manage/refresh.py` → `manage/worklists.py` → `pool.candidates()`, which
    excludes `pool.seen_qids()` (`batch*_approved.json`, `recycle_queue.json`,
    `cut_log.json`) and `bot/data/tagged_in_master.txt`;
  - `manage/status.py` (`bot/state/bot_state.json`);
  - `catalog/build_join_fabric.py` (`bot/data/tagged_in_master.txt`) and
    `catalog/tag_with_mathlib.py` (`bot/data/corrections.jsonl`, from a local
    checkout);
  - `poll.py --decide` run in a `main` checkout.

  To fold back (`git restore --source` also carries deletions):

      git fetch origin bot-state
      git switch -c bot/fold-state origin/main
      git restore --source=origin/bot-state --staged --worktree -- bot/state \
        bot/data/corrections.jsonl bot/data/tagged_in_master.txt bot/data/tagged_pairs.txt
      git diff --cached    # review, commit, open a PR

- To fix the state by hand (for example after a clobber), commit on `bot-state`,
  never on `main`, and push while no poller run is in progress. A push during a
  run makes that run's save fail rather than overwrite your edit; dispatch again
  afterwards.

      git fetch origin bot-state
      git worktree add ../bot-state-edit origin/bot-state
      # edit ../bot-state-edit/bot/state/bot_state.json, commit there
      git -C ../bot-state-edit push origin HEAD:bot-state
      git worktree remove ../bot-state-edit

- Tick from the workflow (`gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean`),
  not a laptop. The laptop poller is retired: two runners with diverging state
  force-pushed over batch 7's reviewed tags in 2026-08. `poll.py --decide` in a
  `main` checkout reads the frozen snapshot, so its answer describes `main`'s
  copy, not the live state.

## Runbook

### A poller run is red

The poller fails closed: a GitHub call that fails, returns an error body or
returns nothing exits non-zero with `POLL_ERROR: …` instead of reading as "wait".
Before this, an expired token kept every run green and the bot blind from
2026-09-21 for three weeks.

- `Bad credentials` / `401` → the token expired or was revoked; see
  [Token rotation](#token-rotation).
- `#N is CLOSED but not merged` → a human decides: reopen the PR, or point
  `bot_state.json` on `bot-state` at the next step.
- `mergeable is still UNKNOWN` → GitHub had not computed mergeability after five
  reads (~30 s). Dispatch again; if it repeats, look at the PR page.
- `RESOLVE-CONFLICT #N: batchN_approved.json MISSING (adopted batch?)` → see
  [Merge conflict on the batch PR](#merge-conflict-on-the-batch-pr).
- `Restore bot state` failed → see [Restore failed](#restore-failed).
- `Commit poll state` failed → someone pushed to `bot-state` mid-run. Read the
  log and dispatch again.

### Restore failed

- `bot-state does not exist` → if the branch ever existed, it was deleted or
  renamed. Do not bootstrap: that re-seeds from `main`'s stale snapshot. If it
  was renamed, rename it back. If it was deleted, recreate it at its last commit:
  1. Find the commit it pointed at when it was deleted:
     `gh api 'repos/Deicyde/WikiLean/activity?ref=bot-state&activity_type=branch_deletion' --jq '.[0].before'`.
     A clone that fetched `bot-state` also has it as `origin/bot-state` or in
     its reflog. Run logs are only a fallback: they show the commits the bot
     restored or pushed, not hand edits pushed between runs, and after a failed
     `Commit poll state` the logged restore commit is older than the one that
     beat it.
  2. GitHub normally still has that commit after the branch is deleted, so
     `gh api repos/Deicyde/WikiLean/git/refs -f ref=refs/heads/bot-state -f sha=<sha>`
     recreates the branch. A clone that has the commit can instead run
     `git push origin <sha>:refs/heads/bot-state`.
  3. Dispatch a new run without the flag. Don't use *Re-run*: a re-run replays
     the original run's commit and inputs.

  Only on the first run after the state moved off `main` is
  `-f bootstrap_state=true` the fix.
- `refusing to bootstrap … on re-run attempt N` → dispatch a new run with
  `-f bootstrap_state=true` if this really is the first run; otherwise see above.
- `outside the state paths` or `has no bot/state/bot_state.json` → someone
  committed other files to `bot-state`, or removed the state file. Fix the
  branch (see the hand-edit steps under [State](#state-the-bot-state-branch))
  and dispatch again.
- `cannot read`, or a bare git `fatal:` line from the fetch or checkout → a
  network or auth error reading the branch. Dispatch again; if it repeats, read
  the log.

### Token rotation

`MATHLIB_FORK_PAT` is the poller's GitHub identity (Deicyde) on both
`Deicyde/mathlib4` and `leanprover-community/mathlib4`.

- **It must be a classic PAT.** A fine-grained PAT cannot write to a public repo
  its owner is not a member of, so it could not comment on or edit the upstream
  PR. A GitHub App would have to be installed on leanprover-community and would
  post as the app.
- **Scopes:** `public_repo` (push the fork branch; open, comment on, edit, close
  and reopen the upstream PR) and `workflow` (the conflict rebuild force-pushes a
  branch rebuilt on fresh upstream master, and GitHub refuses a push that updates
  `.github/workflows/` from a token without it).
- **Current expiry:** not recorded. The token set on 2026-06-23 most likely
  expired on 2026-09-21 (90 days). Write the new date here when you rotate.
- **Tracked by:** Jack (@Deicyde). Set a reminder a week before the expiry.

Steps:

1. As Deicyde, open <https://github.com/settings/tokens> → *Generate new token
   (classic)*. Name it after the poller, pick the two scopes above, and set an
   explicit expiry.
2. `gh secret set MATHLIB_FORK_PAT --repo Deicyde/WikiLean` and paste the token at
   the prompt (never on the command line or in a file).
3. `gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean`, then watch the run:
   Decide should print `POLL_DECISION=act` or `wait` with no `POLL_ERROR`.
4. Revoke the old token if it still exists, and update the expiry line above in a PR.

### Merge conflict on the batch PR

- What the bot does: when the settled PR is `CONFLICTING` and Mathlib master has
  moved since its last rebuild (`conflict_sha` in `bot_state.json`), the next tick
  rebuilds the branch from fresh master, re-applies every tag it carries (no build;
  Mathlib CI compiles), force-pushes, and refreshes the tag table and PR body. The
  log says `RESOLVE-CONFLICT #N: rebuild off fresh master with the K tags it carries`.
  Trimming recycled tags stays with the settle.
- It records the master it rebuilt onto before pushing, so it tries at most once
  per master commit. Master moves many times a day, so a failed rebuild is retried
  on a later tick; to retry now, dispatch the workflow.
- The run fails with `batchN_approved.json MISSING (adopted batch?)` → the rebuild needs the decl
  and file of every tag on the PR. Reconstruct `bot/state/batchN_approved.json` from
  the PR diff on `bot-state`, then dispatch.
- After a clean rebuild, read the new diff before asking anyone to review it.

### Batch PR stuck

The bot never pings anyone. Editing its table comment notifies nobody new, so an
open, green PR can sit unseen.

1. `gh pr view N --repo leanprover-community/mathlib4 --json state,mergeable,labels,reviews,statusCheckRollup`.
   Clear a conflict (above) or a CI failure first. The bot handles the fork-CI
   cache flake by itself once per PR (`retriggered_pr`).
2. Jack posts one plain comment: the per-tag review status, and a request for a
   reviewer's `maintainer merge` or any maintainer's review or `bors d+`. Any
   maintainer on <https://leanprover-community.github.io/teams/maintainers.html>
   can merge.
3. If nothing moves, ask on the Lean Zulip rather than waiting in the queue.

### Cadence

- The schedule says every 30 minutes, but GitHub treats schedules as best-effort.
  Since 2026-08-27 it has run this one about every 4 hours (median gap 4.2 h,
  longest about 11 h). Dispatch the workflow after a merge or a review to react
  sooner.
- GitHub disables a public repo's scheduled workflows after 60 days without
  repository activity. Re-enable it from the Actions tab.
- Upstream latency is Mathlib review: batches 1-5 merged in 1.5-5.4 days, batch 6
  took 42 days.
