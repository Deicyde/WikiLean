# Nightly scheduling (launchd)

Runs the nightly jobs automatically on Jack's Mac. They must run as the
logged-in user (the Claude **Max-plan** login the agent SDK uses lives in the
user's login keychain — a cloud cron can't reach it).

## Files
- `nightly-moderate.sh` — the wrapper launchd executes. Sets absolute PATH/env
  (launchd gives a bare environment), takes a single-instance lock, runs
  **flush → wp-update → review**, logs to `site/cache/cron/moderate-<ts>.log`.
  Limits are env-overridable: `WIKILEAN_{WPUPDATE_LIMIT,REVIEW_LIMIT,CONCURRENCY,BUDGET_TOKENS}`
  (defaults 300 / 15 / 2 / 700000).
- `org.wikilean.moderate.plist` — the LaunchAgent. Fires `03:00` local; if the
  Mac is asleep it runs once on next wake (and `flush` recovers any missed work).
- `newtags-nightly.sh` + `org.wikilean.newtags.plist` — NEW-article tagging @
  03:10 (own lock; see the script header).
- `brain-nightly.sh` + `org.wikilean.brain.plist` — the BRAIN refresh @ 02:20
  (see below).

## Brain nightly (org.wikilean.brain @ 02:20)

`brain-nightly.sh` refreshes the Brain before the annotation jobs wake up
(docs/BRAIN-V2.md "Nightly brain sync"): **ingest external DBs** (daily: nlab /
proofwiki / oeis / stacks / tag-harvest / crossrefs; weekly: lmfdb / eom /
planetmath / wikidata descriptions; monthly: kerodon / dlmf / mathworld —
cadence stamp files live in `site/ops/logs/`) → **agent team**
(`brain/sync_agents.py`, gated `WIKILEAN_BRAIN_AGENTS=1`, OFF by default;
writes `brain/proposals/*.jsonl` only) → **fold → build_nodes → build_edges →
test_acceptance** (RED aborts the publish, old shards stay live) →
**build_shards** → **clean-tree-gated deploy** (gated `WIKILEAN_BRAIN_DEPLOY=1`,
OFF by default; deploys ONLY if `git status --porcelain -- wiki/src
wiki/package.json wiki/wrangler.jsonc` is empty AND `npx tsc --noEmit` passes —
never ships uncommitted Worker WIP). Every step is fail-soft with its own loud
log line; ingest adapters are atomic-write, so a failed fetch keeps the
previous data. Tunables in `nightly.env` (`WIKILEAN_BRAIN_*`, `KERODON_MAX_FETCH`,
`DLMF_MAX_FETCH`, `BRAIN_EXT_NODE_CAP`); logs in `site/ops/logs/brain-<ts>.log`;
lock `.lock.brain.d` (4h stale recovery).

Install (same pattern as the moderate job; the Full Disk Access grant below
covers this job too):

```sh
cp site/ops/org.wikilean.brain.plist ~/Library/LaunchAgents/
launchctl bootout  gui/$(id -u)/org.wikilean.brain 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.wikilean.brain.plist
# run it now (instead of waiting for 02:20):
launchctl kickstart -k gui/$(id -u)/org.wikilean.brain
# watch:
tail -f site/ops/logs/brain-*.log
```

## Install
```sh
cp site/ops/org.wikilean.moderate.plist ~/Library/LaunchAgents/
launchctl bootout  gui/$(id -u)/org.wikilean.moderate 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.wikilean.moderate.plist
# run it now (instead of waiting for 03:00):
launchctl kickstart -k gui/$(id -u)/org.wikilean.moderate
# watch:
tail -f site/cache/cron/moderate-*.log
```

## REQUIRED one-time permission — Full Disk Access for `/bin/bash`
The repo lives under `~/Desktop`, which macOS **TCC** shields from background
(launchd) processes. Without this grant the job fails to even start
(`Operation not permitted` / exit 126). Grant it once:

  **System Settings → Privacy & Security → Full Disk Access → `+` → ⌘⇧G →
  `/bin/bash` → enable.**

bash is the LaunchAgent's "responsible process", so the child processes (the
venv Python → `claude` → node) inherit its disk + keychain access. (Verified:
a full review runs and authenticates under launchd with this grant.) If the
repo ever moves off `~/Desktop` (e.g. to `~/LEAN`), this grant is no longer
needed and can be removed.

Note: the plist's `StandardOutPath`/`StandardErrorPath` point at
`~/Library/Logs/WikiLean/` (off Desktop) — launchd itself can't write onto the
Desktop even with the bash grant.

## Operate
```sh
launchctl print gui/$(id -u)/org.wikilean.moderate | grep -i 'last exit'   # health
launchctl kickstart -k gui/$(id -u)/org.wikilean.moderate                  # run now
launchctl bootout   gui/$(id -u)/org.wikilean.moderate                     # disable
```
A failed batch can be undone with the run-level revert (see the run id in the
log): `curl -X POST .../api/admin/revert-run/<run_id> -H "Authorization: Bearer <PIPELINE_TOKEN>"`.

## Manual trigger (use leftover Max capacity near a window reset)
The exact 5-hour window reset isn't readable by a background script (only the
`/usage` view shows it), so "fire 30 min before reset" can't be fully automated
on the VSCode-extension setup. Instead, fire it yourself in one command when you
see you're near reset with capacity left:

```sh
bash site/ops/run-now.sh        # detached; reviews up to 100 (WIKILEAN_REVIEW_LIMIT)
# or alias it:  alias wlmod='~/Desktop/LEAN/WikiLean/site/ops/run-now.sh'  →  wlmod
```
It runs in your login context (Max auth, no FDA needed), detaches (survives
closing the terminal), and the runner aborts cleanly when the window is spent.
Bind `run-now.sh` to a Raycast script or macOS Shortcut for a literal hotkey.

The **nightly 3 AM launchd run stays installed as a fallback** for days you
forget — both share the same wrapper + single-instance lock, so they never
double-run.
