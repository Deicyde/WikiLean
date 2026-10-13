# Operations runbook

Where WikiLean's scheduled jobs run, where they log, and how to diagnose them,
starting with the nightly review outage that began on 2026-09-18. Commands are
for the nightly host unless a section says otherwise. `site/ops/README.md`
covers installing and configuring the launchd jobs; this file covers checking
on them.

## What runs where

Fill in the host fields once; everything below refers to them.

| Field | Value |
|---|---|
| Nightly host (machine name) | `____________` |
| macOS user that owns the LaunchAgents | `____________` |
| Checkout the jobs run from (`$C` below) | `____________` |
| Checkout commit, and the date it was read | `____________` |
| Python the job uses (`$PY` below, printed as `PY=` on line 2 of each moderate log) | `____________` |
| Mathlib checkout given to Agent 2 (`WIKILEAN_MATHLIB`) | `____________` |

The 2026-09-23 and 2026-09-26 handoffs place the original checkout at
`/Users/jack/Desktop/LEAN/WikiLean` on "older local `main`". Nothing in the repository
records which machine runs the jobs today or at which commit.

| Job | When | Where | Entry point | What it writes |
|---|---|---|---|---|
| `org.wikilean.moderate` | 03:20 host local time (07:20 UTC in EDT) | nightly host, launchd | `site/ops/nightly-moderate.sh` | D1 through the Worker API: stage-0 re-pins, review saves, `pipeline_runs` rows, proposal decisions. Production KV `declcites:v1` and `libdecls:v1` through `npx wrangler kv key put --remote` (`nightly-moderate.sh:176-207`, on unless `WIKILEAN_GRAPH_DEPLOY=0`). Local files under `site/cache/`, `site/out/`, `manage/data/`. |
| `org.wikilean.newtags` | 03:10 | not known to be installed on any host | `site/ops/newtags-nightly.sh` | new articles (PUT) |
| `org.wikilean.brain` | 02:20 | not known to be installed on any host | `site/ops/brain-nightly.sh` | shadow builds only; never deploys |
| Worker drift cron | 06:17 UTC daily | Cloudflare | `wiki/src/drift.ts` (`wiki/wrangler.jsonc` triggers) | `latest_revid`, `wp_drifted` |
| `wikidata-poll.yml` | several times a day | GitHub Actions | `bot/poll.py` | mathlib4 batch PR, `bot/state/` |
| `backup-d1.yml` | daily | GitHub Actions | `wiki/scripts/backup-d1.sh` | backup artifacts |

One moderate night runs, in order: the `manage/refresh.py` control-plane refresh, a
flush of checkpointed writes, the Wikidata embedding refresh, `moderate.py wp-update`
(stage-0, no agents), the formalize review when `manage/formalize_backlog.py` finds a
verified backlog, then `moderate.py review` (15 articles, concurrency 2), proposal
auto-decide, the crossref and coverage refresh, the two KV pushes, and golden-fixture
re-renders. Every `moderate.py` invocation registers one `pipeline_runs` row.

## Log paths

On the nightly host. Paths are relative to the checkout `$C` unless absolute.

| Path | What it holds |
|---|---|
| `site/cache/cron/moderate-YYYYMMDDTHHMMSS.log` | One per night, last 30 kept. Line 2 prints `PY=` and the limits. The review section starts at `--- review batch (search-verified) ---`; each article prints `[i/15] <slug> <error or result> err=N`. |
| `site/cache/cron/skips.log` | Nights skipped because the previous run still held the lock. |
| `site/cache/cron/.lock.d/` | The single-instance lock. A lock older than 4 hours is taken over. |
| `site/cache/.moderate_run.log` | One JSON record per review or new article: `run_id`, `slug`, `error`, `elapsed_s`, `tokens`. **This is where the error text lives.** |
| `site/cache/.decisions.jsonl` | One line per article per pass: `ts`, `run_id`, `mode`, `slug`, `tokens`, `anchors`, `outcome`. A failed review is `"outcome": "error"` with no error text; join on `run_id` to `.moderate_run.log`. |
| `site/cache/.wp_update_report.jsonl` | One line per stage-0 article whose anchors failed (the stage-1/2 candidates). |
| `site/cache/.pending_posts/` | Finished review payloads whose write failed; the next run flushes them. |
| `~/Library/Logs/WikiLean/moderate.out.log`, `moderate.err.log` | launchd stdout/stderr for plists rendered by `site/ops/nightly-launchd.py`. Plists from before 2026-09-05 log to `launchd.out.log` and `launchd.err.log` in the same directory. |

Without host access, the only evidence is public: the `/stats` Pipeline runs table
(all-time totals per kind), `/recent-changes`, and `/<slug>/history`. A review night
that fails on every article shows up as review runs +1, articles +15, errors +15, tokens
unchanged.

## Diagnosis checklist for the nightly host

Run these in Terminal inside the host's logged-in desktop session, not over SSH. The
LaunchAgent runs in that session, and Claude Code keeps its login in the user's login
keychain, which an SSH session may not be able to unlock.

1. **Is the job loaded, and from which checkout?**

   ```bash
   launchctl print "gui/$(id -u)/org.wikilean.moderate" \
     | grep -E 'state =|program|arguments|stdout path|stderr path|runs =|last exit code'
   plutil -p ~/Library/LaunchAgents/org.wikilean.moderate.plist
   ```

   The script path in `arguments` names the checkout. Set `C` to it:

   ```bash
   C=/absolute/path/to/the/checkout
   ```

2. **Which commit is the checkout on?**

   ```bash
   git -C "$C" log -1 --format='%h %ci %s'
   git -C "$C" status --short | head -20
   git -C "$C" merge-base --is-ancestor b079500b HEAD 2>/dev/null \
     && echo "has the 2026-08-04 abort fix (b079500b)" \
     || echo "older than b079500b, or that commit was never fetched"
   grep -nE '^(REPO|PY)=|^export (PATH|WIKILEAN_MATHLIB)=' "$C/site/ops/nightly-moderate.sh"
   ```

   Wrappers from before 2026-09-05 hardcode `REPO`, `PY`, `PATH` and `WIKILEAN_MATHLIB`
   (the last `grep` prints them). Current wrappers derive them in
   `site/ops/nightly-runtime.sh` and read `site/ops/nightly.local.env`.

3. **What did the newest night log?**

   ```bash
   L="$(ls -t "$C"/site/cache/cron/moderate-*.log | head -1)"; echo "$L"
   sed -n 1,3p "$L"
   sed -n '/--- review batch/,/^review: [0-9]* processed/p' "$L" | head -40
   tail -5 "$C/site/cache/cron/skips.log" 2>/dev/null
   ```

4. **What are the error strings for the last runs?** The summary script reads only the
   local logs. It is stdlib-only and runs under Python 3.9, so an older checkout can run
   the current version straight from `origin/main` without changing its working tree:

   ```bash
   git -C "$C" fetch origin
   git -C "$C" show origin/main:manage/nightly_health.py | python3 - --cache "$C/site/cache" --runs 5
   ```

   It prints each review run's distinct errors with counts, example slugs, how long the
   failing articles ran, a likely cause from the table below, the slugs that failed in
   more than one run, and the drift trigger (next section). The raw records, if you prefer:

   ```bash
   tail -n 30 "$C/site/cache/.moderate_run.log" | python3 -c '
   import json, sys
   for line in sys.stdin:
       r = json.loads(line)
       print(r.get("run_id"), r.get("slug"), r.get("elapsed_s"), r.get("error"))'
   grep '"mode": "review"' "$C/site/cache/.decisions.jsonl" | tail -n 15
   ```

5. **Can the job's environment run the pieces review needs and wp-update doesn't?**

   ```bash
   PY=/the/python/from/line/2/of/the/log
   JOBPATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin   # the PATH the wrapper sets
   # extract_sections.py runs under the bare name python3 (site/batch_annotate.py:60)
   env -i HOME="$HOME" PATH="$JOBPATH" /bin/sh -c 'command -v python3 && python3 -c "import sys; print(sys.version)"'
   # the claude CLI the agent SDK runs: the copy bundled in the wheel, else claude on PATH
   "$PY" -m pip show claude-agent-sdk | head -2
   B="$("$PY" -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')"
   ls -l "$B" || env -i HOME="$HOME" PATH="$JOBPATH" /bin/sh -c 'command -v claude'
   "$B" --version
   # one call the way the nightly makes it (spends a few tokens)
   env -i HOME="$HOME" USER="$USER" LOGNAME="$USER" PATH="$JOBPATH" \
     "$B" -p 'Reply with the single word ok' --model claude-opus-4-7 --max-turns 1 --output-format json
   ```

   The last command prints one JSON result. `"is_error": false` with a nonzero `usage`
   means the login, the plan and the model all work from the job's environment. To see
   which account and plan the CLI is logged in as, run `claude` and type `/status`.

6. **Is Agent 2's working directory there, and is there disk space?**

   ```bash
   M=/the/WIKILEAN_MATHLIB/value   # nightly.local.env, the plist, or the old wrapper's export
   ls -d "$M/Mathlib" && git -C "$M" log -1 --format='%h %ci'
   df -h "$C" "$HOME"
   ```

7. **Confirm the fix with one real article,** run the way the nightly runs it:

   ```bash
   cd "$C/site" && env -i HOME="$HOME" USER="$USER" LOGNAME="$USER" PATH="$JOBPATH" \
     WIKILEAN_MATHLIB="$M" "$PY" moderate.py review --limit 1 --concurrency 1
   ```

   This is a production write. On success it saves the article as an
   `ai-moderate:review:<run_id>` revision and registers a run, so `/stats` review tokens
   rise and `/recent-changes` lists the revision.

## What each outcome means

Match the error text from step 4 (`.moderate_run.log`, or the per-article lines in the
moderate log). "Median time" is how long the failing articles ran (`elapsed_s`).

| Error text contains | Meaning | Fix |
|---|---|---|
| `Not logged in`, `Invalid API key`, `Please run /login`, `OAuth`, `401`, `authentication` | The CLI the job runs has no usable Claude login. Median time is a few seconds. | In the host's desktop session, as the job's user, run `claude` and `/login` with the Max account; confirm the plan is active on claude.ai. Re-run step 5's one-call check, then step 7. |
| `Credit balance is too low` | An `ANTHROPIC_API_KEY` reached the SDK, so it billed the API account instead of the Max login. | Find where it is set: `launchctl getenv ANTHROPIC_API_KEY`, the plist's `EnvironmentVariables`, `site/ops/nightly.local.env`. The wrapper unsets it at `nightly-moderate.sh:25`; a wrapper without that line needs updating. |
| `hit your limit`, `weekly limit`, `usage limit`, `resets` | The Max allowance is used up. | Check usage in the CLI (`/status`). On a checkout with b079500b such a night stops after at most 7 errors and the wrapper retries a 5-hour-window limit once; 15 of 15 with this text means the checkout predates b079500b (step 2). |
| `issue with the selected model`, `model:` with `not_found` | The plan or account cannot use the pinned model (`claude-opus-4-7`, `site/batch_annotate.py:57`). | Set `WIKILEAN_AGENT_MODEL` to a model the plan offers, exported from `site/ops/nightly.local.env` (current wrapper) or the plist's `EnvironmentVariables` (older wrapper). Re-run step 5 with that `--model`. |
| `Working directory does not exist` (a mathlib4 path) | Agent 2's working directory is gone. Agent 1 already ran, so median time is a minute or more and Max quota was spent, although `/stats` records 0 tokens. | Restore the checkout or point `WIKILEAN_MATHLIB` at a readable mathlib4 checkout. Never edit that checkout. |
| `extract_failed`, or `No such file or directory: 'python3'` | `extract_sections.py` could not run under the `python3` on the job's PATH. Median time is under a second. | Make step 5's first command succeed: `xcode-select --install` or `sudo xcodebuild -license accept` if `/usr/bin/python3` is the one found, or install a Python 3 in `/opt/homebrew/bin`. |
| `Command failed with exit code`, `Claude Code not found`, `CLIConnectionError`, a JSON decode error | The CLI did not start or crashed. | Run `"$B" --version` (step 5) to see why. Reinstall the same SDK version in the job's venv: `"$PY" -m pip install --force-reinstall "claude-agent-sdk==<version from pip show>"`. |
| `fetch_failed`, `get_failed_<code>`, `network` | Wikipedia or the Worker was unreachable from the job. | wp-update uses the same network paths, so check whether its section of the same log also failed. |
| `agent1_no_json`, `agent2_no_json` | An agent finished without returning JSON. If every article shows it, the CLI is probably returning an error message as its answer. | Run step 5's one-call check and read the result text. |
| `job_crashed: <exception>` | A Python exception escaped the per-article code. | The exception names the cause; it may be a bug to fix in `site/moderate.py`. |
| Different errors per article, the same slugs failing every night | Article-specific failures at the head of the review queue. A failed review never stamps `last_reviewed_at`, so `/api/work` hands out the same articles the next night. | Fix the articles named, or skip them by hand until a runner fix rotates the queue. |
| No error at all, but no run that night | launchd did not start the job, or the lock skipped it. | Step 1 (`runs`, `last exit code`), `skips.log`, and the launchd stderr log. |

Updating an old checkout is Jack's decision. That checkout carries local data changes
the handoffs say to preserve, so prefer a fresh checkout of `main`. Configure it per
`site/ops/README.md` (`nightly.local.env`, `nightly-runtime.sh check moderation`,
`nightly-launchd.py install`), then switch the LaunchAgent with `launchctl bootout` and
`launchctl bootstrap`.

## The review outage that began on 2026-09-18

### What is known

- Every nightly review run since the 2026-09-18 night has taken 15 articles and recorded
  15 errors and 0 tokens. One review run registers per night, so no retry ran. Live
  `/stats` read the review row as 117 runs / 1,761 articles / 843 errors on 2026-10-12
  22:17 UTC and 118 / 1,776 / 858 on 2026-10-13 19:37 UTC, with tokens 26,345,645 both
  times.
- The newest `ai-moderate:review` revision on the site is from 2026-09-17 09:05 UTC
  (run `286ceaa1`).
- The stage-0 wp-update step of the same job still re-pins articles and records no new
  errors.
- The host's logs and commit can't be read remotely, so the cause is not confirmed. The
  ranking below comes from the code.

### What the code says about that pattern

1. **0 tokens does not mean the agents never ran.** `annotate_one` sets `tokens` only
   on its success path (`site/batch_annotate.py:612-616`). Every error return (`:453-460`,
   `:499-502`, `:549-551`, `:617-618`) leaves it unset, and `process_review` returns
   before the POST (`site/moderate.py:934-935`). A failed article is always recorded
   with 0 tokens, even when both agents ran.
2. **The abort and retry rules narrow the error text.** `run_jobs` aborts on the first
   error containing `credit balance` or `authentication`, and after five in a row
   containing `error result`, `rate`, `limit` or `overloaded` (`site/moderate.py:1200-1219`).
   These are substring matches, so `rate` also matches "moderate". Since b079500b
   (2026-08-04), `_AbortingSem` (`site/moderate.py:139-163`) stops queued jobs, so an
   aborted run records at most `ABORT_AFTER` + concurrency = 7 errors. Before that commit
   the flag was set but every queued job still ran, which is how limit nights "burned
   15/15" (b079500b's message). Either way an abort exits 3 (`site/moderate.py:1243`),
   and the wrapper reruns only on exit 3 with `hit your limit`, `usage limit` or
   `resets <digit>` in the log tail (`site/ops/retry-lib.sh:50-55`). So:
   - on a checkout with b079500b, the 15 errors contain none of those keywords;
   - on an older checkout, the errors contain no keyword, or contain `authentication`,
     `credit balance`, or a limit notice without the retry signature (a 5-hour-window
     notice would have produced a second review run that night, and none appears).
3. **wp-update proves most of the stack works.** Stage-0 runs from the same checkout and
   Python, uses the same bearer token and Worker endpoints, fetches from Wikipedia, runs
   the anchor matcher in-process, and writes to `site/cache/` (`site/update_from_upstream.py`,
   `process_slug`). Review alone uses: the agent SDK and the `claude` CLI with its stored
   login (`site/batch_annotate.py:226-268`), the pinned model (`:57`), Agent 2's working
   directory, the Mathlib checkout (`:56`, `:544-548`), and a `python3` subprocess for
   `extract_sections.py` (`:60`, `:217-223`, `:457`).
4. **The review command itself starts.** The run registers, so `batch_annotate` (and
   with it `claude_agent_sdk`) imported (`site/moderate.py:1376`) and `GET /api/work`
   answered.

### Ranked failure modes

1. **The job's Claude login stopped working (most likely).** When the CLI has no usable
   login it fails every article within seconds, and the SDK surfaces its text as
   `RuntimeError: agent_error: Not logged in · Please run /login` or `Invalid API key ·
   Please run /login` (`site/batch_annotate.py:259-268`). Neither contains an abort
   keyword, so the result is 15 of 15, exit 0 and no retry on any checkout, which is the
   observed pattern. The login is the most changeable thing review depends on: OAuth
   expiry or revocation, a plan change, a CLI update elsewhere rewriting the stored
   credentials, or a keychain the launchd session can't read. A 401
   `authentication_error` would instead abort at once on a current checkout (2 or 3
   errors, not 15). *Confirms it:* login wording in step 4, failures under ~10 s, and
   the same failure from step 5's one-call check.
2. **The checkout predates b079500b (2026-08-04).** That brings back the documented
   zero-token modes as 15-of-15 nights: `Credit balance is too low` from an
   `ANTHROPIC_API_KEY`, an `authentication_error`, or a weekly-limit notice. The handoffs
   call the original checkout "older local main". Review runs since 2026-08-04 add up to
   exactly 64 × 15 articles with no shortened run, but a healthy current checkout gives
   the same total, so that is weak evidence. *Confirms it:* step 2.
3. **Agent 2's Mathlib checkout is missing.** Wrappers from before 2026-09-05 hardcode
   `WIKILEAN_MATHLIB=/Users/jack/Desktop/LEAN/mathlib4` with no preflight (the wrapper at
   13915083, line 17). A current wrapper refuses to start without it
   (`site/ops/nightly-runtime.sh:122-140`), which would also stop wp-update. So this
   needs an older wrapper. The SDK raises `Working directory does not exist`, which has
   no abort keyword. Agent 1 runs first, so each failure spends real Max quota while
   recording 0 tokens. The host was short of disk during the September Brain migration
   (5.8 GB free at the 2026-09-23 checkpoint), which makes a moved or deleted checkout
   conceivable. *Confirms it:* the directory error in step 4, failures taking a minute
   or more, step 6.
4. **`python3` on the job's PATH stopped working.** `site/batch_annotate.py:60` runs
   `extract_sections.py` with the bare name `python3` whenever the runner's interpreter
   path contains `venv`, which it always does here. So it resolves through the job's
   PATH: `/opt/homebrew/bin`, then `/usr/bin` (`site/ops/nightly-runtime.sh:62`, and line
   16 of older wrappers). On that Mac the ambient `python3` is the macOS system Python
   (2026-09-26 handoff), the Xcode Command Line Tools shim, which fails after an OS or
   Xcode update until the tools are reinstalled or the license accepted. The result is
   `extract_failed` within a second, 0 tokens and no keyword. wp-update never starts
   `python3`. *Confirms it:* `extract_failed` in step 4 and a failing first command in
   step 5.
5. **The pinned model is refused** (`claude-opus-4-7`, `site/batch_annotate.py:57`). The
   CLI names the model in its error, with no abort keyword. Less likely, because the ID
   is still listed as active. *Confirms it:* a model error in step 4, and step 5 failing
   with `--model claude-opus-4-7` but passing with another model.
6. **CLI or SDK drift.** Current SDK wheels run their bundled CLI before any `claude` on
   PATH (`claude_agent_sdk/_internal/transport/subprocess_cli.py`, `_find_cli`, checked
   at 0.2.144). An auto-update of the interactive CLI doesn't change what the nightly
   runs, but a reinstall of the job's venv would. Drift mostly reaches the nightly
   through shared login state (item 1). *Confirms it:* `Command failed with exit code`,
   `Claude Code not found` or a JSON decode error in step 4, and `"$B" --version` in
   step 5.

Unlikely:

- **Full disk.** wp-update caches article HTML and appends to `.wp_update_report.jsonl`
  every night, and its error count on `/stats` has not moved.
- **A missing MCP server.** The search tools are an in-process SDK server. If building it
  fails, the error is swallowed (`site/batch_annotate.py:287-291`) and Agent 2 just runs
  without them; it doesn't fail the article.
- **The Worker, Wikipedia or the pipeline token.** wp-update shares them.
- **Usage limits on a current checkout.** They stop the run within 7 errors, and the
  5-hour-window notice triggers a retry. Neither happened.

## Drift: measuring the stage-0 trigger

`docs/ROADMAP.md` gates re-anchoring stages 1-2 on "stage-0 clears <90%". Step 4's
script measures it from the host's logs over the last 30 days (`--days N` to change):

- **re-pinned:** wp-update lines in `.decisions.jsonl` with `"outcome": "posted"`;
- **anchors failed:** wp-update `noop` lines whose `anchors.matched` < `anchors.total`
  (the same articles appear in `.wp_update_report.jsonl`, and the script prints that
  count as a cross-check);
- **clear rate:** re-pinned ÷ (re-pinned + anchors failed), both per attempt and per
  distinct slug. Already-current articles, missing `latest_revid` and fetch errors are
  not drift attempts.

It also lists the slugs that failed every attempt in the window, most-retried first.
`/api/work` hands wp-update at most 100 drifted articles, oldest review stamp first
(after flagged and human-edited ones), and a failed re-pin stamps nothing, so those slugs hold the head of the queue night after night.
