# Nightly scheduling (launchd)

Runs the nightly jobs automatically on Jack's Mac. They must run as the
logged-in user (the Claude **Max-plan** login the agent SDK uses lives in the
user's login keychain — a cloud cron can't reach it).

## Files
- `nightly-launchd.py` + `launchd-plist.template` — validate host-local runtime
  paths and render/install all three absolute LaunchAgent plists without loading
  them. Validation uses a sparse launchd-like environment, not interactive-shell
  `WIKILEAN_*`/`BRAIN_*` exports.
- `nightly-moderate.sh` — moderation at 03:20; if the Mac is asleep launchd runs
  it once on next wake. It runs **flush → wp-update → review**, logs to
  `site/cache/cron/moderate-<ts>.log`, and accepts the documented limit overrides.
- `newtags-nightly.sh` — NEW-article tagging @ 03:10 (own lock; see the script
  header).
- `brain-nightly.sh` — the BRAIN refresh @ 02:20 (see below).

## Brain nightly (org.wikilean.brain @ 02:20)

`brain-nightly.sh` refreshes the Brain before the annotation jobs wake up
(docs/BRAIN-V2.md "Nightly brain sync"): **ingest external DBs** (daily: nlab /
proofwiki / oeis / stacks / tag-harvest / crossrefs; weekly: lmfdb / eom /
planetmath / wikidata descriptions; monthly: kerodon / dlmf / mathworld —
cadence stamp files live in `site/ops/logs/`) → **agent team**
(`brain/sync_agents.py`, gated `WIKILEAN_BRAIN_AGENTS=1`, OFF by default;
writes `brain/proposals/*.jsonl` only) → **canonical Wikidata request plan → sealed
entity acquisition when nonempty → offline fold → snapshot/cells/frontier/page builds
and acceptance tests** → **content-addressed release freeze and independent
verification** → **atomic current/previous public staging** → **Worker typecheck
and unit tests**. The Brain page is copied from the same frozen release as its
assets, never from mutable `site/out`. The nightly is shadow-only and rejects the
retired `WIKILEAN_BRAIN_DEPLOY` path before ingest/build work. Production activation
uses `brain-promote-release.sh` with one exact external release and one exact immutable
non-Brain public baseline whose bytes match the reviewed Git-native source attestation.
It requires one externally backed-up, production-pinned receipt root, seals the staged
assets and dry-run Worker bundle,
atomically journals intent, repeats the Git/status/selector fence, and deploys the
sealed bundle with `--no-bundle` exactly once. The post-deploy canary checks the
selector, frozen manifest/page, required explorer assets, a deterministic shard,
REST/MCP release identity, cursor behavior, compatibility aliases, and representative
shell/search-index baseline files. Deployment requires an explicit approved invocation;
automatic rollback is not implemented because Wrangler has no compare-and-swap primitive.
Ordinary catalog ingest is fail-soft, but Wikidata planning/acquisition/folding and every
build/release/deploy gate fail closed and log loudly. See
`docs/BRAIN-RELEASE-RUNBOOK.md` for recovery.
Public staging hashes and copies through a fixed 1 MiB buffer, enforces object,
byte, per-file, and free-space limits from `nightly.env`, and never prunes the
frozen release store automatically. Machine-readable release, SQLite, and public
stage metrics are written under `site/out/`. Logs live in
`site/ops/logs/brain-<ts>.log`; lock `.lock.brain.d` is never stolen based on age. If it
remains after a crash, first prove no Brain job owns it, then remove that exact directory
manually. A private mode-0700 `.brain-run.*` directory holds only the request plan and
captured acquirer stdout; normal cleanup removes those known files, while unexpected residue
is deliberately retained for inspection. Content-addressed entity bundles persist under
`catalog/.cache/wikidata/entity-bundles/`. Their directory ID binds the exact canonical
receipt and lineage bytes, including audit clocks; clock-free receipt/lineage logical IDs
remain stable across an unchanged re-acquisition, while the fresh evidence generation gets
a distinct immutable directory and freshness timestamp.
The Brain job also requires Python 3.12+ and an explicit readable
`BRAIN_MATHLIB_CHECKOUT=/absolute/path/to/mathlib4/Mathlib`. Put host-local
paths in the gitignored `site/ops/nightly.local.env`; see the runbook.
The Wikidata acquirer strips proxy configuration and forces direct HTTPS. A proxy-only host
will fail closed, so verify direct egress to `www.wikidata.org` before relying on a scheduled
non-empty plan. Responses are individually capped, but aggregate transcript generation and
verification are currently in-memory; keep plans bounded pending streaming hardening.
Community-edge graduation is separately off by default. To enable it, set
`WIKILEAN_COMMUNITY_HARVEST=1` and point `WIKILEAN_D1_SNAPSHOT_BUNDLE` at one
absolute, existing, sealed bundle produced by the explicit D1 acquisition step.
The moderation job never acquires live D1 state itself and preserves the prior
`community_edges.jsonl` if that bundle is absent or invalid.

The proposal-entity bundle is not source-plan authority by itself. Its reviewed receipt,
lineage, request preimages, and normalized object still need explicit v3 current-corpus plan
binding. The legacy Wikidata universe, relation-edge, and description ingests also remain
three separate live generations; replacing them with one shared sealed acquisition is still
pending. Nightly remains shadow-only throughout and never promotes production.

Run the 18 focused nightly shell tests after changing this gate:

```sh
python3 site/ops/test_brain_nightly.py
```

P1B activation-review tooling is implemented but has not been run operationally.
Promoter dry-run can retain its exact sealed public/Worker/config inputs and raw read-only
production probes in an external content-addressed store. `brain_activation_bundle.py
context|freeze|verify` runs the exact Node 22/Python 3.12 Worker and Python CI gates itself
with explicit absolute Git/Node/npm/Python tools and no caller `PATH`,
binds the candidate and semantic-baseline releases, immutable public baseline and source
attestation, release/shadow metrics, complete seven-path semantic-diff v2 report, promoter
dry-run, two-worktree context, and CI receipt into exactly 11 canonical evidence files in
the external `WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE`. It freezes a fresh fixed-setting
SQLite measurement, requires an externally anchored non-self semantic baseline, and
re-verifies the retained
dry-run bytes plus the entire non-Brain public baseline. Verification requires the
referenced companion retained-artifact root. These commands do not deploy.
Generating the first real bundle remains blocked until Jack merges P1A onto `main` and
authorizes the launch-context Mathlib and interpreter paths; see the release runbook.

## Install the three nightly jobs
```sh
# First copy/edit the gitignored host-local config. Python and both Mathlib
# settings must be absolute; the check runs from / with launchd-like isolation.
cp site/ops/nightly.local.env.example site/ops/nightly.local.env
python3 site/ops/nightly-launchd.py check
python3 site/ops/nightly-launchd.py install

# The installer seals the exact version-checked Python path into every plist,
# including when it was auto-discovered from this checkout's .venv.

# Equivalent one-time explicit path overrides are sealed into the rendered
# plists (no token is ever written there):
# python3 site/ops/nightly-launchd.py install \
#   --python /absolute/path/to/python3 \
#   --mathlib /absolute/path/to/mathlib4

# Installation only writes validated absolute plists; loading remains an
# explicit operator action.
launchctl bootout  gui/$(id -u)/org.wikilean.brain 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.wikilean.brain.plist
launchctl bootout  gui/$(id -u)/org.wikilean.moderate 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.wikilean.moderate.plist
launchctl bootout  gui/$(id -u)/org.wikilean.newtags 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.wikilean.newtags.plist
# run it now (instead of waiting for 03:20):
launchctl kickstart -k gui/$(id -u)/org.wikilean.moderate
# watch:
tail -f site/cache/cron/moderate-*.log
tail -f site/ops/logs/brain-*.log
```

## Conditional one-time permission — Full Disk Access for `/bin/bash`
If a checkout lives under `~/Desktop`, macOS **TCC** shields it from background
(launchd) processes. Without this grant such a job fails to even start
(`Operation not permitted` / exit 126). Grant it once:

  **System Settings → Privacy & Security → Full Disk Access → `+` → ⌘⇧G →
  `/bin/bash` → enable.**

bash is the LaunchAgent's "responsible process", so the child processes (the
venv Python → `claude` → node) inherit its disk + keychain access. A checkout
under `~/projects` normally does not need this grant. All three plists are
rendered from the current checkout, so moving it requires rerunning
`nightly-launchd.py install`.

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
# or alias the absolute path of this checkout's site/ops/run-now.sh
```
It runs in your login context (Max auth, no FDA needed), detaches (survives
closing the terminal), and the runner aborts cleanly when the window is spent.
Bind `run-now.sh` to a Raycast script or macOS Shortcut for a literal hotkey.

The **nightly 03:20 launchd run stays installed as a fallback** for days you
forget — manual and scheduled moderation share the same wrapper +
single-instance lock, so they never double-run.
