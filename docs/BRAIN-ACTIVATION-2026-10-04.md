# Brain production activation: 2026-10-04

This is the record of the first production promotion of an immutable Brain release.
Every value outside the final section can be checked publicly: against the live site
(read on 2026-10-13), the PR bodies cited, or the code at `main` `37b7036d`. The final
section lists what only the deployment journal and the operator host know. Fill it in
from the journal; never infer those values.

## What production serves

The selector [`/assets/brain/current.json`](https://wikilean.jackmccarthy.org/assets/brain/current.json)
(schema `wikilean.release-selector/v2`):

| Field | Value |
|---|---|
| `release_id` | `sha256:8305822b0083a086ed75c8d39ba05b319ef70417cc209d87ffe6cab090b3f08e` |
| `manifest_sha256` | `fa645b2ba9566fbc1deca1c6ad20f376e69c81e31601a5d24fc3654b59f2ea3e` |
| `manifest` | `/assets/brain/releases/fa645b2b…/release.json` |
| `audited_at` | `2026-10-04T17:25:04Z` |
| `previous_*` | absent |

The served `release.json` is 879,671 bytes, and its SHA-256 equals `manifest_sha256`.
Its identity fields:

| Field | Value |
|---|---|
| `schema` | `wikilean.release/v1` |
| `profile` | `brain-offline-replay-v1` (full offline replay, not the `brain-current-v1` compatibility profile) |
| `semantic_epoch` | `brain-v3-current` |
| `authority.git_commit` | `2eb31baa4459d42a94a882185c748a8a9bac1d3c`, the merge of PR #43 (2026-10-04 11:02 UTC) |
| `authority.through_changeset` | `null` |
| `reducer.version` / `reducer.schedule` | `full-offline-replay/v1` / `sealed-reducer-inventory` |
| `replay.offline_pack_id` | `sha256:294b1fc7fa44c91fa7ba5b365b92cb36a9ec8fa4dc18b95cc9ec6e2970ee1c21` |
| `replay.generation_id` | `sha256:b3016decb994f73ae62b6ac06788f55440a0edcb4accbb4ac78d804bf6fb4b79` |
| `source_set_root` | `sha256:162aa162f2ae5b6a71ccf08736fefd5247ea488fc044bc138f6484bf10924654` |
| `attestations` | one `build`, one `validation` |

The offline pack `294b1fc7…` is not the 2026-09-23 pack `5fa8e3d5…` that ROADMAP and the
September handoffs describe. No other tracked file or PR body (#35–#45) names it.

The REST API reports the same release: `GET /api/brain/search?q=group` returns
`release_id` `sha256:8305822b…` with `snapshot.generated_at` `sha256:b3016dec…` (the replay
generation ID) and `snapshot.pin` `sha256:9dbb5d68…` (a source digest). MCP tool responses
carry the same `release_id` field (`wiki/src/mcp.ts`); no MCP response was captured for this
record. Under this profile the
snapshot echo carries digests, not the build time and Mathlib revision that the MCP
instructions (`wiki/src/mcp.ts`) and `docs/BRAIN-API.md` describe.

## Timeline

| When (UTC) | Event | Evidence |
|---|---|---|
| 2026-10-02 | Jack accepted the refreshed reviewed tool generations | PRs #38, #39 |
| 2026-10-03 | Jack approved extending the reproducibility gate's provenance exception, scoped to private local replay | PR #41 |
| 2026-10-04 11:02 | PR #43 merged as `2eb31baa`, the release's authority commit | `git log` |
| 2026-10-04 17:25:04 | Promotion: the selector's `audited_at`; attempt `20261004T172504Z-8305822b0083-27c7c461cb` | live selector, PR #44 |
| 2026-10-04 | The promoter's canary failed on `/brain` for 300 s; the attempt was journaled `manual_intervention_required` | PR #44 |
| 2026-10-08 06:42 | PR #44 merged as `bf6b4e4a`: the canary no longer advertises `text/html` | PR #44 |
| 2026-10-08 18:25 | PR #45 merged as `37b7036d`: release-neutral merge policy and `semantic-diff/v3` | PR #45 |

## The canary failure

The deploy landed, but the promoter's release-qualified canary reported:

```text
release did not converge within 300s after 57 attempt(s): immutable asset does not match release manifest: /brain
```

Production served the frozen `/brain` page plus 367 bytes. Cloudflare Web Analytics
auto-injects a `static.cloudflareinsights.com/beacon.min.js` script into HTML responses, but
only when the request's `Accept` header includes `text/html`. The canary sent
`application/json,text/html;q=0.9,*/*;q=0.1`. With `Accept: */*` the page is byte-exact.

PR #44 changed the canary's `Accept` to `application/json,*/*;q=0.1`, added a regression
test that no canary request advertises `text/html`, and made mismatch errors report declared
and observed byte counts. Per its body, the patched canary then ran read-only against
production for this release and the public baseline: ok on attempt 1, 40 requests, `/brain`
byte-exact, all 15 baseline assets matching, 12 s. The runbook's Canary section now asks for
a production fetch of `/brain` with the canary's exact headers before any promotion.

## The open promotion attempt

Attempt `20261004T172504Z-8305822b0083-27c7c461cb` was journaled `manual_intervention_required`
(PR #44). While any attempt is incomplete, the promoter refuses every later promotion
(runbook, "If an attempt is incomplete"). PR #44's plan was to reconcile it from clean `main`
after merge. No commit, PR or doc records that this happened, so treat the attempt as open
until the journal shows a final-state record.

Reconciliation is read-only and must run on the host that holds the journal. The promoter's
recovery check (`_check_recovery_checkout` in `site/ops/brain_promote_release.py`) accepts a
clean checkout of current `main` or a clean detached checkout of `2eb31baa`. PR #45's
release-neutral rule does not block it. From the runbook:

```bash
bash site/ops/brain-promote-release.sh \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --reconcile-attempt 20261004T172504Z-8305822b0083-27c7c461cb \
  --approval-note "Jack approved reconciliation of 20261004T172504Z-8305822b0083-27c7c461cb"
```

## Rollback

- The selector has no `previous_*` fields. The public tree holds no release-qualified
  previous Brain release, so rollback is not a selector switch.
- Before this promotion, production ran a Worker with no selector. The last recorded
  observation is Cloudflare deployment `d2926a43-483b-4447-b570-d067c4d77d98`, deployed
  2026-08-28T02:55:59Z, with `current.json` returning 404 and the API reporting no release ID
  ([2026-09-26 handoff](BRAIN-SQLITE-HANDOFF-2026-09-26.md)). That is an observation from
  2026-09-26, not a journal read.
- The authoritative predeploy Worker version is in the attempt's fsynced intent record. When
  the journal is unavailable, Cloudflare's deployment history is the version source:
  `npx --no-install wrangler deployments list --json` and `versions list --json`, run from
  `wiki/` on a host with Wrangler installed and authenticated.
- `site/ops/brain-canary.py` requires `--expected-release-id` and `--expected-manifest-sha256`,
  so it cannot verify a rollback to the pre-selector Worker. Verify that state by hand: the
  selector returns 404 again and `/api/brain/*` responses carry no `release_id`.
- To roll forward to `8305822b…` after a rollback, restore the recorded candidate Worker
  version with `wrangler rollback <version-id>`. This release cannot be re-promoted through
  the promoter from current `main` (next section).
- The P1C rollback and roll-forward drill has not been run, and recovery time is unmeasured.

## Constraints from PR #45's release-affecting rule

`site/ops/brain_release_authority.py` lets the promoter and bundle freezer run from a clean
`main` that has moved past a release's authority commit, but only if `main` descends from it
and every changed path is release-neutral. The neutral set is `docs/`, every `README.md`,
`test_*.py`, `.github/`, `.claude/`, `scripts/`, `bench/`, `bot/`, `manage/`,
`wikifunctions/`, `wiki/test/`, `wiki/e2e/`, `brain/authority/fixtures/`, `site/ops/` (except
`nightly.env` and `brain-nightly.sh`), `CLAUDE.md`, `HANDOFF.md`, `LICENSE`, `.gitignore`,
and `.gitattributes`, with one exception: `site/test_frontier_page.py` is release-affecting.
Everything else is release-affecting by default, including Worker source and configuration,
`wiki/scripts/` (other than `test_*.py`), lockfiles, non-test `site/*.py`, site assets, reducer
code, and Markdown outside those paths. `AGENTS.md` and `CONTRIBUTING.md` are currently
release-affecting. The module itself is the authority for this list.

What that means for this release:

1. **No reviewed `main` can re-promote this release.** `git diff --name-only 2eb31baa 37b7036d`
   includes `brain/tools/reproducibility_gate.py` and `brain/tools/semantic_diff.py` from PR
   #45's `brain/tools` commit (`70301fa0`), and both are release-affecting. At `bf6b4e4a`, the
   promoter predates the rule and requires `HEAD` and `main` to equal `2eb31baa` exactly. At
   `2eb31baa`, the promoter runs that commit's canary, which still advertises `text/html` and
   fails on `/brain` while Web Analytics injection is on. Any future promotion therefore needs
   a new release built at current `main`. Reconciliation of the open attempt is unaffected.
2. **The activation bundle verifies only with `2eb31baa`'s tooling.** At `2eb31baa`, the
   bundle tool uses `semantic-diff/v2` and `build-context/v1`. Main's tool accepts only
   `semantic-diff/v3` and `build-context/v2` (`site/ops/brain_activation_bundle.py`), and PR #45 states that v2 bundles verify only
   with the tool version that produced them. Keep a `2eb31baa` checkout alongside the
   evidence.
3. **Every Worker change rides a new release.** A cache-key bump or `/about` copy edit in
   `wiki/src` needs a new release built at the new commit, a fresh P1B, and a Jack-gated
   promotion. PR #45 puts the rebuild chain at about 5.5 hours and a fresh P1B at about 30
   minutes. No Worker-only hotfix path is documented.
4. **Do not deploy with plain `npm run deploy`.** It runs `wrangler deploy` with
   `assets.directory` `./public` (`wiki/wrangler.jsonc`), which replaces the selector and the
   whole asset tree with whatever the local `wiki/public` holds. Production Worker and asset
   changes go through `site/ops/brain-promote-release.sh` and the
   [release runbook](BRAIN-RELEASE-RUNBOOK.md).

## Frozen inputs

- Production Brain data and its declaration, suffix and premise indexes are frozen at this
  release's inputs. Nothing on `main` automates the next replay release:
  `brain/tools/build_release.py` emits `brain-offline-replay-v1` only when it is handed a
  verified replay, and otherwise freezes the `brain-current-v1` compatibility profile that
  the nightly builds.
- The tracked `brain/data/*.jsonl` files are the 2026-08-28 generation (`git log --
  brain/data/nodes.jsonl`), not this release's graph.
- As of the last committed record (ROADMAP, 2026-09-26), every private and public
  source-policy decision was pending, and PR #41's approval covered private local replay
  only. No committed record shows a public-release source-policy decision for manifest
  `fa645b2b…`.

## Operator must fill

Only the deployment journal and the operator host know these values. Copy them from the
journal and stores; do not reconstruct them.

| Item | Value |
|---|---|
| Journal outcome for attempt `20261004T172504Z-8305822b0083-27c7c461cb` (still open, `deployed_reconciled`, or other), with the date | |
| Event-chain hash of that attempt | |
| Text of Jack's promotion approval (`--approval-note`) and first-deploy approval (`--first-deploy-approval`; required at `2eb31baa` because no selector existed) and the exclusive window | |
| Reconciliation approval text, if reconciled | |
| Full public baseline ID and root (PR #44 cites the prefix `sha256:0ee63469…`) | |
| Activation bundle ID and root | |
| Retained promoter dry-run root | |
| Semantic baseline release ID (PR #45 cites the prefix `8328d804…`) | |
| Predeploy Worker version ID (from the intent record) | |
| Candidate Worker version ID now serving production | |
| Canonical operator host: the machine holding the receipt root, the `fa645b2b…` release store, the baseline and bundle stores, offline pack `294b1fc7…`, and the replay runtime | |
| Receipt roots on other hosts that are non-canonical | |
| Backups: are the receipt root and stores in host backups, where, and when was a restore last checked? | |
| Public-release source-policy record for manifest `fa645b2b…`, or "none" | |
