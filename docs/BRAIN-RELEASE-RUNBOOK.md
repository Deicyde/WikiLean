# Brain release runbook

This runbook covers Phase 1 static Brain releases for `wikilean`. Exact-release
promotion is implemented, but production activation remains blocked on the reviewed P1B
evidence bundle and Jack-gated P1C rollout/rollback drill. Keep the nightly
`WIKILEAN_BRAIN_DEPLOY=0`.

## Safety model

An exact frozen release is eligible only after all existing data/page gates and these
release gates pass:

1. `brain/tools/build_release.py` freezes a content-addressed release in `site/out/brain-releases/<64hex>/`.
2. `brain/tools/verify_release.py` independently verifies the frozen bytes and attestations.
   The current `brain-current-v1` profile requires the WLBN SQLite schema v2 and
   path-specific media/logical formats; legacy schema-v1 indexes are not publishable.
3. `site/ops/brain_public_baseline.py` freezes every non-Brain Worker asset into a
   separate content-addressed, read-only baseline. Required shell files and the
   declaration, suffix, and premise indexes must all be present and exactly close
   over their manifest-declared shards; Brain-owned paths are forbidden. The bytes
   must match the canonical `wiki/public-asset-source-attestation.json` blob in the
   exact authority commit, so ignored or dirty `wiki/public` output cannot self-attest.
4. `wiki/scripts/build-public.ts` copies only that verified baseline into a fresh
   external tree, then overlays the requested release, exact retained production
   release, byte-identical aliases, selector, and Brain page. Mutable or ignored
   checkout output cannot leak into a promotion.
5. Worker typecheck/unit tests run, Wrangler emits a reviewed bundle in dry-run mode,
   and a second no-bundle dry-run proves the sealed bundle and external asset tree are
   accepted together. Node 22 and the installed Wrangler version must match the lockfile.
6. The promoter's no-mutation dry-run retains its exact sealed public tree, Worker
   bundle, Wrangler configuration, and raw selector/status/history responses in a
   separate content-addressed, read-only store. The proposed intent is rebased to those
   durable bytes instead of the temporary promotion workspace.
7. `site/ops/brain_activation_bundle.py freeze` runs the exact Worker and Python CI gates
   itself from the clean promotion checkout using explicitly approved absolute Git,
   Node, npm, and Python executables. It reruns the fixed nightly SQLite probe and freezes
   that fresh measurement, then binds the receipt plus ten other evidence documents into
   one immutable, content-addressed review bundle. Neither operation deploys.
8. The promoter atomically publishes a durable intent, then re-verifies the releases,
   baseline, sealed public tree, sealed bundle, configuration, clean `main`, and
   production as Worker status A → exact selector A → Worker status A.
9. Wrangler runs once from the sealed bundle with `--no-bundle --strict`, an attempt-unique
   tag, and an attempt-unique message. The candidate version is adopted only when those
   annotations, 100% traffic, the exact staged selector bytes, and the canary agree.
10. `site/ops/brain-canary.py` waits for selector, manifest, required view assets,
   cell manifest/shard, `/brain`, `/brain.html`, REST API/cursor, MCP, aliases, and
   representative files from the frozen non-Brain baseline to agree.

The nightly script derives the repository root from its own physical location. Do not copy
`brain-nightly.sh` outside the checkout and invoke that copy. The presence of the promoter
is not authorization to deploy; P1C remains an explicit Jack gate.

## Activation prerequisites

Before running even a shadow build, create the gitignored
`site/ops/nightly.local.env` with a readable, read-only Mathlib tree, Python 3.12+,
and the reviewed absolute activation-tool paths (or set the same variables in the
invoking environment):

```bash
cp site/ops/nightly.local.env.example site/ops/nightly.local.env
```

Then replace the example placeholders in that file. For a one-off interactive
run, the equivalent environment is:

```bash
export BRAIN_MATHLIB_CHECKOUT=/absolute/path/to/mathlib4/Mathlib
export WIKILEAN_PYTHON=/absolute/path/to/python3.12
export WIKILEAN_BRAIN_RECEIPT_DIR=/absolute/private/backed-up/deploy-receipts
export WIKILEAN_BRAIN_PUBLIC_BASELINE_STORE=/absolute/path/to/public-baselines
export WIKILEAN_BRAIN_PROMOTER_DRY_RUN_STORE=/absolute/private/promoter-dry-runs
export WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE=/absolute/path/to/activation-bundles
export WIKILEAN_BRAIN_GIT=/absolute/path/to/git
export WIKILEAN_BRAIN_NODE=/absolute/path/to/node
export WIKILEAN_BRAIN_NPM=/absolute/path/to/npm
```

Initialize the one canonical deployment journal root once, before any dry-run or
promotion. Its immutable marker pins the directory to production; do not create a
second root to bypass an incomplete attempt:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_deploy_journal.py init \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --repo-root "$PWD" \
  --target-origin https://wikilean.jackmccarthy.org
```

The job fails closed before fold/build if the Mathlib tree is unset or missing.
The optional proposal agents use a separate interpreter
(`WIKILEAN_BRAIN_AGENT_PYTHON`, default `catalog/.venv/bin/python3`) and are
skipped with an explicit warning if that environment is absent. Verify all configured
paths from the same launch context that will run the job; missing paths fail closed.

The tooling is ready, but evidence generation is not yet authorized: Jack must first merge
the reviewed P1A changes onto `main` and approve the exact read-only Mathlib checkout and
Git/Node/npm/Python executable paths. Do not substitute this feature branch or an arbitrary
project-local Mathlib dependency for that reviewed host context.

## Shadow release

The nightly is unconditionally shadow-only; keep the retired flag explicit:

```bash
cd /Users/jackmccarthy/projects/WikiLean
WIKILEAN_BRAIN_DEPLOY=0 bash site/ops/brain-nightly.sh
```

The run log is written under `/Users/jackmccarthy/projects/WikiLean/site/ops/logs/`. A green shadow run leaves the verified release staged in `/Users/jackmccarthy/projects/WikiLean/wiki/public/assets/brain` and writes its machine-readable builder result to `/Users/jackmccarthy/projects/WikiLean/site/out/brain-release-result.json`; production is unchanged.

The same run also writes:

- `site/out/brain-release-metrics.json`: the read-only/immutable
  `brain/tools/measure_store.py` report—release/projection identities, size and
  page use, table/stream counts, `ANALYZE` state, integrity checks, bounded query
  plans/latencies, total duration, and process high-water RSS;
- `site/out/brain-public-result.json`: staged release ID, retained release IDs,
  object/byte totals, duration, process maximum RSS, fixed copy-buffer size, and
  free space before/after staging, plus the verified frozen page digest.
- Promoter canary evidence is stored in its external durable journal: the converged
  release/baseline, attempts, duration, request count, checked response bytes, and
  process maximum RSS.

Public artifacts are hashed and copied with a fixed 1 MiB buffer; neither the
current nor previous namespace is materialized in memory. Before writing a
candidate, staging requires space for the complete candidate plus
`BRAIN_PUBLIC_MIN_FREE_BYTES` (256 MiB by default). Object, total-byte, and
per-file limits are also configurable in `site/ops/nightly.env`. A failed limit,
digest, or copy leaves the prior public tree unchanged.

To verify a frozen release independently:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" brain/tools/verify_release.py \
  --manifest /Users/jackmccarthy/projects/WikiLean/site/out/brain-releases/<release-hex>/release.json \
  --root /Users/jackmccarthy/projects/WikiLean/site/out/brain-releases/<release-hex>
```

## Freeze the non-Brain public baseline

Promotion never trusts the ignored `wiki/public` directory directly. The first baseline
requires a reviewed, Git-native inventory. Generate the complete public tree and all
three search-index families exactly once, render its canonical attestation, and commit
that attestation for review:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/export_wikidata_rdf.py
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/build_static_pages.py
(cd wiki && npm ci)
(cd wiki && node --experimental-strip-types scripts/build-public.ts \
  --brain-release-manifest /absolute/release-store/<bootstrap-release-hex>/release.json \
  --brain-release-dir /absolute/release-store/<bootstrap-release-hex>)
(cd wiki && npm run build:indexes)

"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_public_baseline.py attest \
  --source-public "$PWD/wiki/public" \
  > wiki/public-asset-source-attestation.json
git add wiki/public-asset-source-attestation.json
git commit -m "Attest reviewed non-Brain public assets"
```

The bootstrap Brain release is excluded from the attestation and is not the promotion
candidate. After the exact inventory is reviewed and lands on `main` as commit C, keep the
generated non-Brain public tree byte-for-byte unchanged—especially the timestamp-bearing
index manifests, and freeze it against C. Separately produce the actual candidate Brain
release through the P1B isolated shadow-build flow at the same commit C; do not use the
bootstrap release and do not rerun `npm run build:indexes`. Any non-Brain byte change
between `attest` and `freeze` is a hard mismatch, not a repair:

```bash
cd /Users/jackmccarthy/projects/WikiLean

AUTHORITY_COMMIT="$(git rev-parse HEAD)"
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_public_baseline.py freeze \
  --source-public "$PWD/wiki/public" \
  --store "$WIKILEAN_BRAIN_PUBLIC_BASELINE_STORE" \
  --repo-root "$PWD" \
  --authority-git-commit "$AUTHORITY_COMMIT"
```

The command prints the exact `baseline_id` and immutable root. Review and retain both.
The freezer excludes `brain.html` and `assets/brain/**`, rejects missing shell/index
assets, validates every manifest-declared shard/name chunk, and publishes only a canonical
manifest plus its complete read-only inventory. Confirm the shadow release result and the
baseline both name C as their authority commit. This repository intentionally does not yet contain the
first attestation; freeze and verify therefore fail closed until P1B completes this workflow.

## Build the P1B activation evidence bundle

Use two non-overlapping worktrees at the same reviewed authority commit: an isolated build
worktree for the shadow nightly and generated assets, and a clean promotion worktree whose
`HEAD` and `refs/heads/main` both equal that commit. Keep all evidence and the final bundle
outside both checkouts:

```bash
export BUILD_WORKTREE=/absolute/path/to/wikilean-p1b-build
export PROMOTION_WORKTREE=/absolute/path/to/wikilean-p1b-promotion
export EVIDENCE_DIR=/absolute/private/p1b-evidence
export WIKILEAN_BRAIN_PROMOTER_DRY_RUN_STORE=/absolute/private/promoter-dry-runs
export WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE=/absolute/private/activation-bundles
export WIKILEAN_BRAIN_GIT=/absolute/path/to/git
export WIKILEAN_BRAIN_NODE=/absolute/path/to/node
export WIKILEAN_BRAIN_NPM=/absolute/path/to/npm
```

After the shadow release, public baseline, and semantic comparison have been produced,
run the promoter's no-mutation path with retention enabled. The retained store must be an
absolute path outside every checkout, release, baseline, receipt, and temporary promotion
workspace:

```bash
cd "$PROMOTION_WORKTREE"
bash site/ops/brain-promote-release.sh sha256:<candidate-hex> \
  --release-root /absolute/releases/<candidate-hex> \
  --public-baseline-id sha256:<public-baseline-hex> \
  --public-baseline-root /absolute/public-baselines/<public-baseline-hex> \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --dry-run \
  --retain-dry-run-store "$WIKILEAN_BRAIN_PROMOTER_DRY_RUN_STORE" \
  > "$EVIDENCE_DIR/promoter-dry-run.json"
```

This publishes a read-only, content-addressed copy of the exact staged public and Worker
trees, Wrangler configuration, and raw selector/status/history responses. Retain that root
alongside the activation bundle; the freezer verifies it before writing the bundle and
again at the final publication fence.

Record the two-worktree context from the promotion checkout:

```bash
cd "$PROMOTION_WORKTREE"
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_activation_bundle.py context \
  --build-worktree "$BUILD_WORKTREE" \
  --promotion-worktree "$PROMOTION_WORKTREE" \
  --git "$WIKILEAN_BRAIN_GIT" \
  > "$EVIDENCE_DIR/build-context.json"
```

The bundle freezer invokes the CI recorder in-process immediately before validation, so a
caller-authored or stale CI JSON file cannot be substituted. Git, Node, npm, and Python
are explicit reviewed absolute paths; the caller's `PATH` is discarded and child-tool
resolution uses private shims to those approved executables. It requires Node 22 and
Python 3.12, a credential-free allowlisted environment, bounded process groups, and
pre/post clean-authority fences. It runs exactly `npm ci`, `npm run test:ci`, and
`PYTHON=<selected> ./scripts/ci-python.sh`; the generated canonical
`wikilean.brain-activation-ci/v2` evidence retains the exact argv, working directory,
return code, tool-version probes, and complete stdout/stderr for every successful gate.
`brain_activation_ci.py` remains useful as a standalone preview, but its output is not a
freeze input.

Generate `semantic-diff.json` with `brain/tools/semantic_diff.py`. Its
`wikilean.semantic-diff/v2` coverage must include exactly these seven release paths:

- `brain/data/nodes.jsonl`
- `brain/data/edges.jsonl`
- `brain/data/edges_links.jsonl`
- `brain/data/cells.jsonl`
- `brain/data/synapses.jsonl`
- `brain/data/frontier.jsonl`
- `brain/data/frontier_graph.json`

Freeze the completed review set from the promotion checkout:

```bash
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_activation_bundle.py freeze \
  --release-manifest /absolute/releases/<candidate-hex>/release.json \
  --semantic-baseline-manifest /absolute/releases/<baseline-release-hex>/release.json \
  --expected-semantic-baseline-id sha256:<baseline-release-hex> \
  --public-baseline-manifest /absolute/public-baselines/<baseline-hex>/manifest.json \
  --source-attestation "$PROMOTION_WORKTREE/wiki/public-asset-source-attestation.json" \
  --release-result "$EVIDENCE_DIR/release-result.json" \
  --release-metrics "$EVIDENCE_DIR/release-metrics.json" \
  --shadow-public-result "$EVIDENCE_DIR/shadow-public-result.json" \
  --semantic-diff "$EVIDENCE_DIR/semantic-diff.json" \
  --promoter-dry-run "$EVIDENCE_DIR/promoter-dry-run.json" \
  --build-context "$EVIDENCE_DIR/build-context.json" \
  --git "$WIKILEAN_BRAIN_GIT" \
  --node "$WIKILEAN_BRAIN_NODE" \
  --npm "$WIKILEAN_BRAIN_NPM" \
  --python "${WIKILEAN_PYTHON:-.venv/bin/python3}" \
  --output-store "$WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE"
```

The bundle contains exactly 11 canonical evidence files:

1. `candidate-release.json`
2. `semantic-baseline-release.json`
3. `public-baseline.json`
4. `public-asset-source-attestation.json`
5. `release-result.json`
6. `release-metrics.json`
7. `shadow-public-result.json`
8. `semantic-diff.json`
9. `promoter-dry-run.json`
10. `build-context.json`
11. `ci-evidence.json`

The freezer checks complete releases, baseline/source identity, a fresh fixed
`--limit 100 --iterations 5 --warmup 1 --check-limit 100` SQLite measurement,
semantic detail and summaries, retained dry-run bytes, clean worktree separation, and its
fresh CI receipt before atomically publishing a read-only content-addressed directory. It
rejects a candidate self-diff and requires the reviewed prior release ID as an external
trust anchor. Verify the returned root independently with both returned IDs and retain its
bundle ID/root for review:

```bash
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_activation_bundle.py verify \
  --bundle-root "$WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE/<bundle-hex>" \
  --expected-bundle-id sha256:<bundle-hex> \
  --expected-semantic-baseline-id sha256:<baseline-release-hex>
```

This entire section is P1B preparation only. `context`, the CI recorder, `freeze`, and
`verify` do not authorize or perform a production deployment. Actual evidence generation
remains blocked until Jack merges P1A and authorizes the host paths above.

The review artifact is an attested two-root set: the 11-file activation bundle plus the
content-addressed retained promoter root named by `promoter-dry-run.json`. Normal `verify`
requires and revalidates that companion root, including every non-Brain public-baseline
file, the Brain release bytes, Worker/config bytes, and raw read-only production probes.
Do not delete or relocate either root after review.

## Deploy

> **Production activation is currently blocked on P1B/P1C review and approval.** The
> exact promoter is available, but running it with `--execute` changes production.

The legacy deploy-enabled nightly has been removed. The nightly rejects any nonzero
`WIKILEAN_BRAIN_DEPLOY` before ingest/build work and contains no Wrangler mutation command.

Run the no-mutation preflight first from a separate clean checkout/worktree at the
release authority commit:

```bash
cd /Users/jackmccarthy/projects/WikiLean
bash site/ops/brain-promote-release.sh sha256:<64hex> \
  --release-root /absolute/release-store/<release-hex> \
  --public-baseline-id sha256:<baseline-hex> \
  --public-baseline-root /absolute/public-baselines/<baseline-hex> \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --dry-run \
  --retain-dry-run-store "$WIKILEAN_BRAIN_PROMOTER_DRY_RUN_STORE"
```

Run the promoter from a separate clean checkout/worktree at the frozen release's recorded
authority commit. Point it at the explicit read-only release root/store produced by the
isolated shadow build. The current shadow reducer writes timestamp-bearing tracked outputs,
so building and promoting from one checkout would violate the clean-tree gate; do not solve
that by ignoring generated dirtiness.

After Jack approves the exact release, baseline, journal location, and exclusive window,
the mutating form is:

```bash
WIKILEAN_BRAIN_DEPLOY=1 bash site/ops/brain-promote-release.sh sha256:<64hex> \
  --release-root /absolute/release-store/<release-hex> \
  --public-baseline-id sha256:<baseline-hex> \
  --public-baseline-root /absolute/public-baselines/<baseline-hex> \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --execute \
  --approval-note "Jack approved release <id>, baseline <id>, and this window"
```

If production has no release-qualified selector, the promoter fails unless the
operator also supplies `--allow-first-deploy-without-selector`. That exception requires
`--first-deploy-approval`, Jack's approval for the exact window, and is written into the
deployment intent record.
TLS, DNS, and timeout failures are never an acceptable substitute for the explicit flag.

Every promotion uses a crash-safe append-only event journal. Before any mutating Wrangler
call, the promoter fsyncs an intent record containing the attempt ID, requested/prior
release IDs, authority commit, and predeploy Worker version. It then appends immutable
invocation, deploy-result, canary, reconciliation, and final-state records linked to that
attempt. A derived summary may be generated but never replaces or mutates the evidence
records.

Gitignored `site/out` is not a durable journal sink. Set
`WIKILEAN_BRAIN_RECEIPT_DIR` to an absolute directory outside the checkout; the promoter
requires the preinitialized target marker and fails if the root is unset, unpinned, or
unwritable. Never switch to a second receipt root to bypass an incomplete attempt, never
garbage-collect these records automatically, include the directory in host backups, and
attach each event-chain hash to the rollout review or incident record. On startup, the
promoter refuses another mutation until every incomplete attempt has been reconciled
against live Wrangler and selector state.

The P1A `--dry-run` calls read-only Wrangler status/history commands and Wrangler's local
`deploy --dry-run` compiler/validator. Wrangler 4.120 performs no authentication or upload
in that mode. The promoter never invokes a mutating deploy in `--dry-run` and has no
automatic rollback path.

Before staging, the script fetches the production `/assets/brain/current.json`. If production names a prior qualified release, its exact frozen directory must still exist and independently verify locally. This prevents a deployment-disabled shadow run from accidentally becoming the retained `previous` release. Immediately before the deploy, the selector must still be byte-identical and the Worker version must remain stable across the status/selector/status sandwich.

The deploy path invokes exactly one strict, tagged `wrangler deploy` using the sealed
external bundle with `--no-bundle` and parses the candidate version from Wrangler's
`Current Version ID`. Once Wrangler has
been invoked, the release-qualified canary always runs, even if the command
returns nonzero or the control-plane response cannot establish candidate
ownership: the remote write may already have landed. The promoter never rolls back
automatically. Any failure before the Wrangler
invocation leaves production unchanged; deployment uncertainty or canary failure returns
nonzero and leaves the journal open for explicit reconciliation or separately approved
manual recovery.

If an attempt is incomplete, every later promotion is blocked. Reconcile it read-only:

```bash
bash site/ops/brain-promote-release.sh \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --reconcile-attempt <attempt-id> \
  --approval-note "Jack approved reconciliation of <attempt-id>"
```

An exact prior state is observed across a quiet interval and again after its canary before
the journal can close. If a durable deploy invocation exists but the candidate is not live,
the quiet interval must be at least the recorded command timeout (900 seconds by default),
no matching local Wrangler process may remain, and attempt-tagged version/deployment history
must be stable before and after the wait. Closing an unchanged predeploy state also requires
`--confirm-no-production-change --no-change-approval "<Jack approval>"`; a stable orphan
version upload is recorded, while any attempt-correlated deployment remains blocking.

For that exact no-change case, rerun with the dedicated approval:

```bash
bash site/ops/brain-promote-release.sh \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --reconcile-attempt <attempt-id> \
  --approval-note "Jack approved reconciliation of <attempt-id>" \
  --confirm-no-production-change \
  --no-change-approval "Jack approved closing the unchanged production state"
```

A stable unrelated deployment remains blocked unless Jack explicitly authorizes
`--accept-external-supersession --external-supersession-approval "<Jack approval>"`; the
same timeout/process/history fence applies, and reconciliation records that it made no
production change. Reconciliation may run from clean current `main` (so reviewed recovery
fixes remain usable) or a clean detached checkout of the historical authority commit; its
Wrangler configuration and toolchain must still match the durable intent.

```bash
bash site/ops/brain-promote-release.sh \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --reconcile-attempt <attempt-id> \
  --approval-note "Jack approved reconciliation of <attempt-id>" \
  --accept-external-supersession \
  --external-supersession-approval "Jack approved the observed external deployment"
```

## Retention and disk pressure

The public tree retains exactly the current and previous qualified releases plus
current-release compatibility aliases. The content-addressed frozen store under
`site/out/brain-releases/` is deliberately **not** garbage-collected by the
nightly. Do not delete from it merely to satisfy the staging headroom check:
current/previous production releases and any release referenced by an overlay,
investigation, publication, or hold must remain recoverable. Inspect usage with
`du -sh site/out/brain-releases wiki/public/assets/brain` and perform any frozen
store cleanup only after a manifest/hold review and a restore drill.

Phase 0 deterministic-input work is still open: wall-clock `generated_at` flows
into the current compatibility snapshot identity. A no-op rebuild can therefore
produce a new immutable release ID. Treat apparently duplicate releases as
distinct until the clean-room reproducibility milestone is complete; do not
automate frozen-store garbage collection around assumed semantic equivalence.

## Canary

Run the same release-qualified canary manually with the full release ID:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain-canary.py \
  --base-url https://wikilean.jackmccarthy.org \
  --expected-release-id sha256:<release-hex> \
  --public-baseline-id sha256:<baseline-hex> \
  --public-baseline-root /absolute/public-baselines/<baseline-hex> \
  --timeout 300 \
  --interval 5
```

Every request carries a unique cache-busting query parameter and `Cache-Control: no-cache`.
Each response is capped at 32 MiB. Success prints versioned JSON including
`attempts`, measured `convergence_seconds`, request/byte counts, and maximum RSS;
the promoter appends it to the durable attempt journal. The initial operational target is
convergence within five minutes; this is a target, not a measured rollback SLO.

The canary requires all of the following to agree on the expected release:

- Strict `current.json` selector and immutable `release.json` identity.
- Cell manifest, a deterministic manifest-declared shard, labels, supercells,
  explorer, and frontier graph.
- Sampled immutable files matching the byte length and SHA-256 declared by `release.json`.
- `/brain` matching the frozen page byte-for-byte, with `/brain.html` accepted only as
  either the same bytes or Cloudflare's exact same-origin HTTP 307 canonicalization to
  `/brain` (the canary never enables general redirects).
- A representative Brain API response with matching `release_id`, snapshot metadata, and a new opaque cursor that advances.
- A representative `POST /mcp` `brain_filter` call with the expected JSON-RPC
  envelope, no tool error, and a payload naming the same release.
- Mutable `cells/`, `sources.json`, and `xref_index.json` samples byte-equal to their immutable current-release counterparts.
- Required shell/shared files and deterministic samples from every declaration, suffix,
  and premise index family byte-equal to the frozen public baseline. HTML assets are
  checked through their canonical served routes (`/concepts` and the permanently reserved
  retired `/map` route for the deployed `404.html` body).

## Wrangler rollback

These commands were verified against the repository-pinned Wrangler `4.120.0`
(`wiki/package.json` and `wiki/package-lock.json`) using `npx --no-install wrangler
--help`, `deployments --help`, `deployments status --help`, `deployments list --help`,
`versions list --help`, and `rollback --help`. Run `npm ci` first when
`wiki/node_modules` is absent. `--no-install` is deliberate: it prevents `npx` from
downloading a newer Wrangler during a recovery.

Inspect the current production deployment as machine-readable JSON:

```bash
cd /Users/jackmccarthy/projects/WikiLean/wiki
npx --no-install wrangler deployments status --json
```

Deployment is permitted only when `versions` contains exactly one entry with
`percentage: 100` before and after reading the selector. A split or otherwise
ambiguous deployment fails closed and must be reviewed manually.

Automatic rollback is deliberately not implemented. Wrangler 4.120 has no rollback
compare-and-swap operation, so rollback is always a separately approved manual action in
an exclusive deployment window.

Roll back noninteractively to a verified version ID:

```bash
cd /Users/jackmccarthy/projects/WikiLean/wiki
npx --no-install wrangler rollback <version-id> --yes \
  --message "Rollback Brain release sha256:<failed-release-hex>"
```

`<version-id>` is the predeploy Worker A UUID/release pair from the fsynced intent record;
it is not the current version returned after B is live and it is not a Brain release ID.
Cross-check that UUID against `npx --no-install wrangler deployments list --json` and
`npx --no-install wrangler versions list --json`. Use `deployments status --json` only to
confirm that candidate B still owns 100% of traffic before mutation. Never substitute a
Brain release hash for a Worker version ID.

After rollback, run the canary against the predeploy Brain release:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain-canary.py \
  --base-url https://wikilean.jackmccarthy.org \
  --expected-release-id sha256:<predeploy-release-hex> \
  --timeout 300 \
  --interval 5
```

Record the emitted `convergence_seconds` in the incident or rollout notes. Do not claim a rollback SLO until repeated measured drills justify one.

## Recovery cases

The P1A promoter leaves a failed candidate in place, appends the observed failure state,
returns nonzero, and prints the manual recovery path. This avoids blindly overwriting an
unrelated newer deployment. Before a manual rollback, establish an exclusive window and
repeat candidate version B → exact candidate selector → candidate version B immediately
before invoking Wrangler. The residual non-CAS race must be accepted explicitly.

If the prior Worker version is absent or ambiguous in the durable intent record, the
promoter does not guess. Inspect `npx --no-install wrangler deployments list --json` and
`versions list --json`, reconcile the intended prior Worker/release pair with the selector
and release records, then run `npx --no-install wrangler rollback <version-id> --yes` and
canary the matching prior Brain release.

After a rollback, roll forward only by restoring the recorded candidate Worker version or
by promoting the same exact frozen release through the approved promoter. Do not rerun the
nightly for roll-forward: it rebuilds and may introduce an unreviewed release.

If the rollback command succeeds but the prior release does not converge, keep the run failed. Reinspect deployment status, fetch `/assets/brain/current.json` with `Cache-Control: no-cache`, confirm the prior immutable namespace exists, and rerun the canary. Escalate rather than repeatedly deploying or deleting release directories.
