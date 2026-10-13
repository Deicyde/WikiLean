# WikiLean daily `@[wikidata]` batch bot

Runs one batch of 10 `@[wikidata]` cross-reference tags at a time through review →
merge-or-recycle against `leanprover-community/mathlib4`. The poller
(`.github/workflows/wikidata-poll.yml` → `poll.py`) is event-driven: it opens the
next batch only after the current one merges.

## The loop

Each run does two phases:

1. **Settle** the current PR (`settle.py`, deterministic):
   - **Gate:** ≥2 distinct human reviewers on the PR **and** ≥24h since it opened.
   - When the gate is open, classify each tag and **split** the PR down to its
     green tags (`split.py`, force-push), then post a ready-to-merge comment.
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
commit there. Its mutable state (`bot/state/` and `bot/data/corrections.jsonl`)
lives on the orphan `bot-state` branch instead, which branch protection does not
cover:

- Each run starts with `bot/ops/state-branch.sh restore`, which copies the branch's
  files over the checkout before anything reads them, and ends with
  `state-branch.sh save`, which commits any change on top of the restored commit
  and pushes it as a fast-forward. A failed read fails the run; it never falls back
  to `main`'s copy.
- If `bot-state` does not exist, the first run creates it from `main`'s
  `bot/state/`. No setup is needed.
- `main`'s `bot/state/` is now a frozen snapshot. The Brain build reads
  `bot/state/{cut_log,seed_queue,recycle_queue,brain_queue}.json` from `main`, so
  it sees the bot's queue changes only after someone folds them back with a
  reviewed PR:

      git fetch origin bot-state
      git switch -c bot/fold-state origin/main
      git checkout origin/bot-state -- bot/state bot/data/corrections.jsonl
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
  force-pushed over batch 7's reviewed tags in 2026-08.

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
- `Commit poll state` failed → someone pushed to `bot-state` mid-run, or the
  branch holds files outside the state paths. Read the log, fix the branch, and
  dispatch again.

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
  rebuilds the branch from fresh master, re-applies the green tags (no build;
  Mathlib CI compiles), force-pushes, and refreshes the tag table and PR body. The
  log says `RESOLVE-CONFLICT #N: rebuild off fresh master with K green tags`.
- It records the master it rebuilt onto before pushing, so it tries at most once
  per master commit. Master moves many times a day, so a failed rebuild is retried
  on a later tick; to retry now, dispatch the workflow.
- `batchN_approved.json MISSING (adopted batch?)` → the rebuild needs the decl
  and file of every green tag. Reconstruct `bot/state/batchN_approved.json` from
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
