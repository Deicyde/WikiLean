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
| `wikidata-poll.yml` | scheduled, every few hours in practice | GitHub Actions | `bot/poll.py` | mathlib4 batch PR, bot state |
| `backup-d1.yml` | daily | GitHub Actions | `wiki/scripts/backup-d1.sh` | backup artifacts |

One moderate night runs, in order: the `manage/refresh.py` control-plane refresh, a
flush of checkpointed writes, the Wikidata embedding refresh, `moderate.py wp-update`
(stage-0, no agents), the formalize review when `manage/formalize_backlog.py` finds a
verified backlog, then `moderate.py review` (15 articles, concurrency 2), proposal
auto-decide, the crossref and coverage refresh, the two KV pushes, and golden-fixture
re-renders. Every real (non-dry) `review`, `wp-update`, `new` or `all` invocation
registers one `pipeline_runs` row; `flush` and dry runs don't.

**Which wrapper the host has matters.** `main` gained the current wrapper
(`site/ops/nightly-runtime.sh`, `nightly.local.env`, plists rendered by
`site/ops/nightly-launchd.py`) on 2026-09-21 in PR #24. A checkout of `main` from before
that date runs the older wrapper, which hardcodes `REPO`, `PY`, `PATH` and
`WIKILEAN_MATHLIB`, has no preflight, and runs several steps with the bare name
`python3`.

## Log paths

On the nightly host. Paths are relative to the checkout `$C` unless absolute.

| Path | What it holds |
|---|---|
| `site/cache/cron/moderate-YYYYMMDDTHHMMSS.log` | One per night, last 30 kept. Line 2 prints `PY=` and the limits. The review section starts at `--- review batch (search-verified) ---`; each article prints `[i/15] <slug> <error or result> err=N`, and the section ends with `review: N processed, N errors, …`. |
| `site/cache/cron/skips.log` | Nights skipped because the previous run still held the lock. |
| `site/cache/cron/.lock.d/` | The single-instance lock. A lock older than 4 hours is taken over. |
| `site/cache/.moderate_run.log` | One JSON record per review or new article: `run_id`, `slug`, `op`, `error`, `elapsed_s`, `tokens`. **This is where the error text lives.** |
| `site/cache/.decisions.jsonl` | One line per article per pass: `ts`, `run_id`, `mode`, `slug`, `tokens`, `anchors`, `outcome`. A failed review is `"outcome": "error"` with no error text; join on `run_id` to `.moderate_run.log`. |
| `site/cache/.wp_update_report.jsonl` | One line per stage-0 article whose anchors failed (the stage-1/2 candidates). |
| `site/cache/.pending_posts/` | Finished review payloads whose write failed; the next run flushes them. |
| `~/Library/Logs/WikiLean/moderate.out.log`, `moderate.err.log` | launchd stdout/stderr for plists rendered by `site/ops/nightly-launchd.py`. The older checked-in plist logs to `launchd.out.log` and `launchd.err.log` in the same directory. |

Without host access, the only evidence is public: the `/stats` Pipeline runs table
(all-time totals per kind), `/recent-changes`, and `/<slug>/history`. A review night
that fails on every article shows up as review runs +1, articles +15, errors +15, tokens
unchanged. Once the host runs a checkout with PR #52, each run's `pipeline_runs.notes`
also carries a sanitized sample of its distinct errors (counts, median time, example
slugs). Read it from a D1 backup or with
`wrangler d1 execute <db> --remote --command "SELECT run_id, started_at, errors, notes FROM pipeline_runs WHERE kind='review' ORDER BY started_at DESC LIMIT 5"`
until the Worker shows recent runs itself.

## Diagnosis checklist for the nightly host

Run these in Terminal inside the host's logged-in desktop session, not over SSH. Claude
Code keeps its login in the user's login keychain. Even in the desktop session,
Terminal's security context is not the LaunchAgent's, so step 5 ends with a check that
runs inside launchd itself.

1. **Is the job loaded, and from which checkout?**

   ```bash
   launchctl print "gui/$(id -u)/org.wikilean.moderate" > /tmp/wl-moderate-job.txt
   grep -E 'state =|program =|stdout path|stderr path|runs =|last exit code' /tmp/wl-moderate-job.txt
   sed -n '/arguments = {/,/}/p' /tmp/wl-moderate-job.txt
   sed -n '/environment = {/,/}/p' /tmp/wl-moderate-job.txt
   ```

   The script path in `arguments` names the checkout. Set `C` to it:

   ```bash
   C=/absolute/path/to/the/checkout
   ```

2. **Which commit and wrapper is the checkout on?**

   ```bash
   git -C "$C" log -1 --format='%h %ci %s'
   git -C "$C" status --short | head -20
   git -C "$C" merge-base --is-ancestor b079500b HEAD 2>/dev/null \
     && echo "has the 2026-08-04 abort fix (b079500b)" \
     || echo "older than b079500b, or that commit was never fetched"
   ls "$C/site/ops/nightly-runtime.sh" 2>/dev/null && echo "current wrapper" || echo "older wrapper"
   grep -nE '^(REPO|PY)=|export (PATH|WIKILEAN_MATHLIB)=' "$C/site/ops/nightly-moderate.sh" "$C/site/ops/nightly-runtime.sh" 2>/dev/null
   ```

3. **What did the newest night log?**

   ```bash
   L="$(ls -t "$C"/site/cache/cron/moderate-*.log | head -1)"; echo "$L"
   sed -n 1,3p "$L"
   PY="$(sed -n 's/^PY=\([^ ]*\).*/\1/p' "$L")"; echo "PY=$PY"
   sed -n '/--- review batch/,/^review: [0-9]* processed/p' "$L" | head -40
   grep -n 'returned [1-9]\|failed' "$L" | head -20
   tail -5 "$C/site/cache/cron/skips.log" 2>/dev/null
   ```

   The last `grep` lists every other step that failed that night. On the older wrapper
   the manage refresh, formalize backlog, crossref, Formal Conjectures, coverage,
   decl-citation, library-decl and golden-fixture steps all run under the bare name
   `python3`. If they fail too, suspect `python3` (outcome table) before anything
   Claude-related.

4. **What are the error strings for the last runs?** The summary script reads only the
   local logs. It is stdlib-only and Python 3.9-compatible, so an older checkout can run
   the current version straight from `origin/main` without changing its working tree.
   Run it with the job's `$PY`, which wp-update proves works:

   ```bash
   git -C "$C" fetch origin
   git -C "$C" show origin/main:manage/nightly_health.py | "$PY" - --cache "$C/site/cache" --runs 5
   ```

   It prints, for each review run:
   - the distinct errors with counts and example slugs;
   - how long the failing articles ran;
   - a likely cause from the table below.

   It also lists the slugs that failed in more than one run, and the drift trigger (last
   section of this file). The raw records, if you prefer:

   ```bash
   tail -n 30 "$C/site/cache/.moderate_run.log" | "$PY" -c '
   import json, sys
   for line in sys.stdin:
       r = json.loads(line)
       print(r.get("run_id"), r.get("slug"), r.get("elapsed_s"), r.get("error"))'
   grep '"mode": "review"' "$C/site/cache/.decisions.jsonl" | tail -n 15
   ```

5. **Can the job's environment run what review needs and wp-update doesn't?**

   ```bash
   # the PATH the wrapper exports (current wrapper first, else the older one)
   JOBPATH="$(sed -n 's/^ *export PATH="\(.*\)"$/\1/p' "$C/site/ops/nightly-runtime.sh" "$C/site/ops/nightly-moderate.sh" 2>/dev/null | head -1)"; echo "$JOBPATH"
   # extract_sections.py runs under the bare name python3 when $PY's path contains
   # "venv" (site/batch_annotate.py:60), otherwise under $PY itself
   env -i HOME="$HOME" PATH="$JOBPATH" /bin/sh -c 'command -v python3 && python3 -c "import sys; print(sys.version)"'
   # the claude CLI the agent SDK runs: the copy bundled in the wheel, else a claude on PATH
   "$PY" -c 'import claude_agent_sdk as s; print(s.__version__)'
   B="$("$PY" -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')"
   [ -x "$B" ] || B="$(env -i HOME="$HOME" PATH="$JOBPATH" /bin/sh -c 'command -v claude')"
   echo "CLI: ${B:-none found}"; "$B" --version
   # one call the way the nightly makes it (spends a few tokens)
   env -i HOME="$HOME" USER="$USER" LOGNAME="$USER" PATH="$JOBPATH" \
     "$B" -p 'Reply with the single word ok' --model claude-opus-4-7 --max-turns 1 --output-format json
   ```

   If no CLI is found, the SDK also looks in `~/.npm-global/bin`, `/usr/local/bin`,
   `~/.local/bin`, `~/node_modules/.bin`, `~/.yarn/bin` and `~/.claude/local`. The last
   command prints one JSON result: `"is_error": false` with nonzero `usage` means the
   login, the plan and the model work **from Terminal**. That does not clear a keychain
   the LaunchAgent can't read. To check that, run the same call as a one-off LaunchAgent:

   ```bash
   mkdir -p ~/Library/Logs/WikiLean
   cat > /tmp/org.wikilean.probe.plist <<EOF
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0"><dict>
     <key>Label</key><string>org.wikilean.probe</string>
     <key>ProgramArguments</key><array>
       <string>$B</string><string>-p</string><string>Reply with the single word ok</string>
       <string>--model</string><string>claude-opus-4-7</string>
       <string>--max-turns</string><string>1</string>
       <string>--output-format</string><string>json</string>
     </array>
     <key>EnvironmentVariables</key><dict><key>PATH</key><string>$JOBPATH</string></dict>
     <key>StandardOutPath</key><string>$HOME/Library/Logs/WikiLean/probe.out.log</string>
     <key>StandardErrorPath</key><string>$HOME/Library/Logs/WikiLean/probe.err.log</string>
   </dict></plist>
   EOF
   launchctl bootstrap "gui/$(id -u)" /tmp/org.wikilean.probe.plist
   launchctl kickstart "gui/$(id -u)/org.wikilean.probe"
   sleep 60; cat ~/Library/Logs/WikiLean/probe.out.log ~/Library/Logs/WikiLean/probe.err.log
   launchctl bootout "gui/$(id -u)/org.wikilean.probe"
   ```

   Run it once with the screen unlocked and once locked if the nightly fires while the
   screen is locked. If the error text in step 4 says `fetch_failed`, also probe
   Wikipedia the way review does. Review fetches the article's **pinned** revision with
   `requests` and its own User-Agent (`site/batch_annotate.py:171-214`). wp-update
   fetches the latest revision through a different code path, so a working wp-update
   doesn't clear this:

   ```bash
   S=<a slug that failed in step 4>
   R="$(curl -s "https://wikilean.jackmccarthy.org/api/article/$S.json" | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["revid"])')"
   ls -l "$C/site/cache/$S.$R.html" 2>/dev/null
   "$PY" -c 'import sys, requests
   r = requests.get("https://en.wikipedia.org/w/api.php", timeout=60,
       params={"action": "parse", "oldid": sys.argv[1], "prop": "revid", "format": "json", "formatversion": "2"},
       headers={"User-Agent": "WikiLean/0.1 (https://github.com/Deicyde/WikiLean; jack.mccarthy.1@stonybrook.edu)"})
   print(r.status_code, r.text[:300])' "$R"
   ```

6. **Is Agent 2's working directory there, is there disk space, and did the OS change?**

   ```bash
   M=/the/WIKILEAN_MATHLIB/value   # nightly.local.env, the plist, or the older wrapper's export
   ls -d "$M/Mathlib" && git -C "$M" log -1 --format='%h %ci'
   df -h "$C" "$HOME"
   softwareupdate --history | head -15
   xcode-select -p; /usr/bin/python3 -V
   ```

7. **Confirm the fix with one real article,** run the way the nightly runs it:

   ```bash
   cd "$C/site" && env -i HOME="$HOME" USER="$USER" LOGNAME="$USER" PATH="$JOBPATH" \
     WIKILEAN_MATHLIB="$M" WIKILEAN_PROPOSALS=1 "$PY" moderate.py review --limit 1 --concurrency 1
   ```

   Add `WIKILEAN_AGENT_MODEL=<model>` if the job's environment sets one (step 1's
   `environment` block, or `nightly.env` / `nightly.local.env`). This is a production
   write:
   - it first flushes any checkpointed writes from earlier runs;
   - on success it saves the article as an `ai-moderate:review:<run_id>` revision and
     registers a run, so `/stats` review tokens rise and `/recent-changes` lists the
     revision.

## What each outcome means

Match the error text from step 4 (`.moderate_run.log`, or the per-article lines in the
moderate log). The "Likely cause" column is what `manage/nightly_health.py` prints.
"Median time" is how long the failing articles ran (`elapsed_s`).

| Error text contains | Likely cause | Meaning | Fix |
|---|---|---|---|
| `extract_failed`, or `No such file or directory: 'python3'` | python3 on the job's PATH | `extract_sections.py` could not run under the `python3` the job's PATH finds. Median time is under a second. | Make step 5's first command succeed: `xcode-select --install` or `sudo xcodebuild -license accept` if `/usr/bin/python3` is the one found, or install a Python 3 earlier on the job's PATH. |
| `Working directory does not exist` (a mathlib4 path) | Mathlib checkout path | Agent 2's working directory is gone. Agent 1 already ran, so median time is a minute or more and Max quota was spent, although `/stats` records 0 tokens. | Restore the checkout or point `WIKILEAN_MATHLIB` at a readable mathlib4 checkout. Never edit that checkout. |
| `Credit balance is too low` | API-key credential | The CLI authenticated with an API key instead of the Max login. The environment variable can't be the source: `site/batch_annotate.py:35-37` removes `ANTHROPIC_API_KEY` at import, and `site/moderate.py:639-645` keeps it removed under the default `--auth subscription`. | Look at the CLI's own credentials: an API-key (Console) login stored by `claude`, `primaryApiKey` in `~/.claude.json`, or an `env` or `apiKeyHelper` entry in `~/.claude/settings.json`. Log in again with the Max account. |
| `Not logged in`, `Invalid API key`, `Please run /login`, `OAuth`, `401`, `authentication` | Claude login | The CLI the job runs has no usable Claude login. Median time is a few seconds. | Log in with the CLI the job runs: start `"$B"` from step 5 in the desktop session and type `/login` with the Max account; confirm the plan is active on claude.ai. Re-run both step 5 checks, then step 7. For a login that doesn't depend on the keychain, `claude setup-token` prints a long-lived token for headless use; export it as `CLAUDE_CODE_OAUTH_TOKEN` from the gitignored `site/ops/nightly.local.env` (current wrapper) or the plist's `EnvironmentVariables` (older wrapper), never from git. |
| `hit your limit`, `weekly limit`, `usage limit`, `resets` | plan or usage limit | The Max allowance is used up. | Check usage in the CLI (`/status`). On a checkout with b079500b, five such errors in a row stop the run within 7 errors, and the wrapper retries a 5-hour-window limit once. 15 of 15 with this text means the checkout predates b079500b (step 2). |
| `issue with the selected model`, `not_found_error` | model id | The plan or account cannot use the pinned model (`claude-opus-4-7`, `site/batch_annotate.py:57`). | Set `WIKILEAN_AGENT_MODEL` to a model the plan offers, exported from `site/ops/nightly.local.env` (current wrapper) or the plist's `EnvironmentVariables` (older wrapper). Re-run step 5 with that `--model`. |
| `network_post_failed`, `rate_limited_429`, `post_<code>` | Worker write | The agents finished but the save to the Worker failed. The payload is checkpointed and the next run flushes it; tokens were counted. | Check the Worker and the network from the host; `moderate.py flush` retries the checkpoints. |
| `Command failed with exit code`, `Claude Code not found`, `error result`, a JSON decode error | claude CLI | The CLI did not start, crashed, or reported an error without text. | Run `"$B" --version` (step 5). A bundled CLI too old for the service needs a newer `claude-agent-sdk` in the job's venv (see `catalog/requirements.txt`); a broken install needs a reinstall of the same version. |
| `fetch_failed` | Wikipedia fetch | Review could not fetch the article's pinned revision. It swallows the exception, so the text says nothing more. | Run step 5's Wikipedia probe and read the status and body. |
| `get_failed_<code>` | Worker read | The Worker refused or failed the article read. | wp-update makes the same read; compare with its section of the same log. |
| `agent1_no_json`, `agent2_no_json`, `render_failed`, `final_read_failed` | agent output | The agents ran but their output could not be used. | Read that article's records; if every article shows it, run step 5's one-call check and read the result text. |
| `job_crashed: <exception>` | unclassified | A Python exception escaped the per-article code. | The exception names the cause; it may be a bug to fix in `site/moderate.py`. |
| Different errors per article, the same slugs failing every night | (per error) | Article-specific failures at the head of the review queue. A failed review never stamps `last_reviewed_at`, so `/api/work` hands out the same articles the next night. | Fix the articles named, or skip them by hand until a runner change rotates the queue. |
| No error at all, but no run that night | | launchd did not start the job, or the lock skipped it. | Step 1 (`runs`, `last exit code`), `skips.log`, and the launchd stderr log. |

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
  22:17 UTC and 118 / 1,776 / 858 on 2026-10-13, with tokens 26,345,645 both times.
- The newest `ai-moderate:review` revision on the site is from 2026-09-17 09:05 UTC
  (run `286ceaa1`).
- The stage-0 wp-update step of the same job still re-pins articles and records no new
  errors.
- The host's logs and commit can't be read remotely, so the cause is not confirmed. The
  ranking below comes from the code.

### What the code says about that pattern

1. **Unchanged tokens place every failure inside `annotate_one`.** `annotate_one` sets
   `tokens` only on its success path (`site/batch_annotate.py:612-616`). Its error
   returns (`:453-460`, `:499-502`, `:549-551`, `:596-598`, `:617-618`) leave it unset,
   even when one or both agents already ran. Failures after `annotate_one` succeeds (the
   final read, or the Worker POST at `site/moderate.py:947-997`) keep the agents' tokens,
   and `run_jobs` adds them up. Flat tokens therefore mean all 15 articles failed during
   fetch, extract, an agent call, or render, never at the save.
2. **The abort and retry rules narrow the error text.**
   - `run_jobs` aborts on the first error containing `credit balance` or
     `authentication`, and after five *consecutive* errors containing `error result`,
     `rate`, `limit` or `overloaded` (`site/moderate.py:1200-1219`). These are substring
     matches, so `rate` also matches "moderate".
   - Since b079500b (on `main` since 2026-08-04), `_AbortingSem`
     (`site/moderate.py:139-163`) stops queued jobs, so an aborted run records at most
     `ABORT_AFTER` + concurrency = 7 errors.
   - Before that commit the flag was set but every queued job still ran, which is how
     limit nights "burned 15/15" (b079500b's message).
   - Either way an abort exits 3 (`site/moderate.py:1243`). The wrapper reruns only on
     exit 3 with `hit your limit`, `usage limit`, `resets <digit>` or `rate_limited_429`
     in the log tail (`site/ops/retry-lib.sh:50-55`; the older inline copy lacked the
     last one).

   So:
   - on a checkout with b079500b, the 15 errors include no auth or credit error and no
     five consecutive limit-type errors;
   - on an older checkout, any text without the retry signature gives 15 of 15. A
     5-hour-window notice carries the signature and would have produced a second review
     run that night; none appears.
3. **wp-update proves part of the stack works.** Stage-0 runs from the same checkout and
   Python, uses the same bearer token and Worker endpoints, runs the anchor matcher
   in-process, and writes to `site/cache/` (`site/update_from_upstream.py`,
   `process_slug`). Review alone uses:
   - the agent SDK and the `claude` CLI with its stored login
     (`site/batch_annotate.py:226-268`);
   - the pinned model (`:57`);
   - Agent 2's working directory, the Mathlib checkout (`:56`, `:544-548`);
   - a `python3` subprocess for `extract_sections.py` (`:60`, `:217-223`, `:457`);
   - a Wikipedia fetch of the **pinned** revision, through `requests` with its own
     User-Agent, every exception collapsed to `fetch_failed` (`:171-214`, `:453-455`).
     wp-update fetches the latest revision through `render.fetch_article_html`.
4. **The review command itself starts.** The run registers, so `batch_annotate` (and
   with it `claude_agent_sdk`) imported (`site/moderate.py:1376`) and `GET /api/work`
   answered.
5. **The host most likely runs the older wrapper.** The current wrapper reached `main`
   on 2026-09-21, after the outage began, and the handoffs call the checkout "older
   local main". That wrapper has no preflight for the Mathlib path and runs several
   steps under the bare name `python3`.

### Ranked failure modes

1. **The job's Claude login is unusable (most likely).** When the CLI has no usable
   login it fails every article within seconds, and the SDK surfaces its text as
   `RuntimeError: agent_error: Not logged in · Please run /login` or `Invalid API key ·
   Please run /login` (`site/batch_annotate.py:259-268`). Neither contains an abort
   keyword, so the result is 15 of 15, exit 0 and no retry on any checkout, which is the
   observed pattern. Credentials that are missing or unreadable fit this:
   - removed by a logout or by a CLI update elsewhere rewriting the stored login;
   - stored where the launchd session can't read them (a keychain Terminal can read
     but the LaunchAgent can't).

   An expired or revoked token that surfaces as a 401 `authentication_error` would
   instead abort at once on a current checkout (2 or 3 errors, not 15). On an older
   checkout it would also give 15 of 15 (item 2). *Confirms it:* login wording in step
   4, failures under ~10 s, and the same failure from step 5's launchd check.
2. **The checkout predates b079500b (2026-08-04).** That brings back the documented
   zero-token modes as 15-of-15 nights: `Credit balance is too low` from an API-key
   credential the CLI holds, an `authentication_error`, or a weekly-limit notice without
   the retry signature. The handoffs call the original checkout "older local main".
   Review runs since 2026-08-04 add up to exactly 64 × 15 articles with no shortened
   run, but a healthy current checkout gives the same total, so that is weak evidence.
   *Confirms it:* step 2.
3. **Agent 2's Mathlib checkout is missing.** The older wrapper hardcodes
   `WIKILEAN_MATHLIB=/Users/jack/Desktop/LEAN/mathlib4` with no preflight (the wrapper at
   13915083, line 17), and that is the wrapper the host most likely runs. The SDK raises
   `Working directory does not exist`, which has no abort keyword. Agent 1 runs first,
   so each failure spends real Max quota while recording 0 tokens. The host was short of
   disk during the September Brain migration (5.8 GB free at the 2026-09-23 checkpoint),
   which makes a moved or deleted checkout conceivable. *Confirms it:* the directory
   error in step 4, failures taking a minute or more, step 6.
4. **`python3` on the job's PATH stopped working.** `site/batch_annotate.py:60` runs
   `extract_sections.py` with the bare name `python3` whenever `$PY`'s path contains
   `venv`. The older wrapper sets `PY` to `catalog/.venv/bin/python3` and `PATH` to
   `/opt/homebrew/bin:/usr/bin:…` (13915083, lines 15-16). On that Mac the ambient
   `python3` is the macOS system Python (2026-09-26 handoff), the Xcode Command Line
   Tools shim, which fails after an OS or Xcode update until the tools are reinstalled or
   the license accepted. The result is `extract_failed` within a second, 0 tokens and no
   keyword. wp-update never starts `python3`, but the older wrapper's other bare-`python3`
   steps would fail the same night. *Confirms it:* `extract_failed` in step 4, failing
   steps in step 3's last `grep`, step 5's first command, and step 6's update history.
5. **Review can't fetch pinned revisions from Wikipedia.** Review's fetch differs from
   wp-update's in library, User-Agent and target revision, and any exception becomes
   `fetch_failed`, which has no abort keyword and takes seconds. *Confirms it:*
   `fetch_failed` in step 4 and a failing Wikipedia probe in step 5.
6. **The pinned model is refused** (`claude-opus-4-7`, `site/batch_annotate.py:57`). The
   CLI names the model in its error, with no abort keyword. Less likely, because the ID
   is still listed as active. *Confirms it:* a model error in step 4, and step 5 failing
   with `--model claude-opus-4-7` but passing with another model.
7. **CLI or SDK drift.** Current SDK wheels run their bundled CLI before any `claude` on
   PATH (`claude_agent_sdk/_internal/transport/subprocess_cli.py`, `_find_cli`, checked
   at 0.2.144). An auto-update of the interactive CLI doesn't change what the nightly
   runs. A bundled CLI can still become too old for the service, and drift reaches the
   nightly through shared login state (item 1). *Confirms it:* the CLI messages in the
   outcome table, and `"$B" --version` in step 5.

Unlikely:

- **Full disk.** wp-update caches article HTML and appends to `.wp_update_report.jsonl`
  every night, and its error count on `/stats` has not moved.
- **A missing MCP server.** The search tools are an in-process SDK server. If building it
  fails, the error is swallowed (`site/batch_annotate.py:287-291`) and Agent 2 just runs
  without them; it doesn't fail the article.
- **The Worker or the pipeline token.** wp-update makes the same reads and writes.
- **A failure at the save.** Flat tokens rule it out (point 1).
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

Dry runs (`wp-update --dry-run`) are left out, because their needs-work lines look
like real ones in `.decisions.jsonl`. With fewer than 20 distinct drifted slugs the
script says the sample is too small instead of judging the trigger.

It also lists the slugs that failed every attempt in the window, most-retried first.
`/api/work` hands wp-update at most 100 drifted articles, ordered flagged first, then
those with the drift flag set, then human-edited, then oldest review stamp. A failed re-pin stamps nothing, so those slugs
hold the head of the queue night after night.
