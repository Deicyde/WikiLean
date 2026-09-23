# Brain release runbook

This runbook covers Phase 1 static Brain releases for `wikilean`. Exact-release
promotion is implemented, but production activation remains blocked on the reviewed P1B
evidence bundle and Jack-gated P1C rollout/rollback drill. Keep the nightly
`WIKILEAN_BRAIN_DEPLOY=0`.

## Safety model

An exact frozen release is eligible only after all existing data/page gates and these
release gates pass:

1. `brain/tools/build_release.py` freezes an exact manifest namespace in
   `site/out/brain-releases/<manifest-sha256>/`. The logical `release_id` remains
   timestamp and attestation independent.
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
   release, byte-identical aliases, selector, and Brain page. Every immutable public
   namespace is named by the SHA-256 of its exact `release.json` bytes, not by the
   timestamp-independent logical release ID. Mutable or ignored checkout output cannot
   leak into a promotion.
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
10. `site/ops/brain-canary.py` waits for the selector's exact manifest digest,
   required view assets,
   cell manifest/shard, `/brain`, `/brain.html`, REST API/cursor, MCP, aliases, and
   representative files from the frozen non-Brain baseline to agree.

The nightly script derives the repository root from its own physical location. Do not copy
`brain-nightly.sh` outside the checkout and invoke that copy. The presence of the promoter
is not authorization to deploy; P1C remains an explicit Jack gate.

## Activation prerequisites

Before running even a shadow build, create the gitignored
`site/ops/nightly.local.env` with a readable, read-only Mathlib tree, Python 3.12+,
an explicit reviewed canonical Wikidata observation plan, and the reviewed absolute
activation-tool paths (or set the same variables in the invoking environment):

```bash
cp site/ops/nightly.local.env.example site/ops/nightly.local.env
```

Then replace the example placeholders in that file. For a one-off interactive
run, the equivalent environment is:

```bash
export BRAIN_MATHLIB_CHECKOUT=/absolute/path/to/mathlib4/Mathlib
export WIKILEAN_WIKIDATA_OBSERVATION_PLAN=/absolute/private/reviewed-wikidata-observation-plan.json
export WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256="<64-lowercase-hex>"
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

The job fails closed before acquisition/fold/build if the Mathlib tree or observation-plan
path is unset or missing. The volume floors are reviewed policy; do not derive them from
the current corpus or substitute the planner's test defaults. Generate a candidate plan
offline from those reviewed floors and the five explicit selectors:

```bash
"${WIKILEAN_PYTHON:-.venv/bin/python3}" brain/plan_wikidata_observation.py \
  --concept-layer "$PWD/catalog/data/concept_layer.jsonl" \
  --grounding "$PWD/catalog/data/rebuild_grounding.json" \
  --prior-brain-nodes "$PWD/brain/data/nodes.jsonl" \
  --universe-extension "$PWD/catalog/data/universe_extension.jsonl" \
  --wikidata-crossrefs "$PWD/catalog/data/wikidata_crossrefs.json" \
  --volume-floors /absolute/private/reviewed-wikidata-volume-floors.json \
  > /absolute/private/candidate-wikidata-observation-plan.json
```

Review the resulting canonical bytes before configuring their absolute path. The plan and
its lowercase SHA-256 are one approved input: configure both
`WIKILEAN_WIKIDATA_OBSERVATION_PLAN` and
`WIKILEAN_WIKIDATA_OBSERVATION_PLAN_SHA256`. Runtime preflight verifies the digest, and the
nightly copies those exact bytes into its private run directory before acquisition. The plan
and its sealed acquisition contract are documented in `brain/authority/README.md`.
The optional proposal agents use a separate interpreter
(`WIKILEAN_BRAIN_AGENT_PYTHON`, default `catalog/.venv/bin/python3`) and are
skipped with an explicit warning if that environment is absent. Verify all configured
paths from the same launch context that will run the job; missing paths fail closed.

P1A is merged on `main`, but evidence generation is not yet authorized: Jack must approve
the exact read-only Mathlib checkout, observation plan, and Git/Node/npm/Python executable
paths. Do not substitute an arbitrary project-local Mathlib dependency or unreviewed plan
for that reviewed host context.

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
  --manifest /Users/jackmccarthy/projects/WikiLean/site/out/brain-releases/<manifest-hex>/release.json \
  --root /Users/jackmccarthy/projects/WikiLean/site/out/brain-releases/<manifest-hex>
```

## Freeze the non-Brain public baseline

Promotion never trusts the ignored `wiki/public` directory directly. The first baseline
requires a reviewed, Git-native inventory. Generate the complete non-Brain public tree and
all three search-index families exactly once, render its canonical attestation, and commit
that attestation for review. The convenience build below requires an existing verified Brain
release. Direct non-Brain assembly, described afterward, has no such dependency:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/export_wikidata_rdf.py
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/build_static_pages.py
(cd wiki && npm ci)
(cd wiki && node --experimental-strip-types scripts/build-public.ts \
  --brain-release-manifest /absolute/release-store/<bootstrap-manifest-hex>/release.json \
  --brain-release-dir /absolute/release-store/<bootstrap-manifest-hex>)
(cd wiki && npm run build:indexes)

export PUBLIC_SOURCE_ROOT="$PWD/wiki/public"
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_public_baseline.py attest \
  --source-public "$PUBLIC_SOURCE_ROOT" \
  > wiki/public-asset-source-attestation.json
git add wiki/public-asset-source-attestation.json
git commit -m "Attest reviewed non-Brain public assets"
```

For direct assembly, use a fresh isolated tree containing exact generator/helper bytes,
committed CSS/JS, the selected Git-pinned `catalog/data/articles.jsonl`, and the complete
canonical annotation sidecars from one verified D1 source export. Record those input and
program identities. The exporter accepts the normalized D1 article object and its retained
SHA-256 as an explicit membership source:

```bash
"$WIKILEAN_PYTHON" "$ASSET_TREE/site/export_wikidata_rdf.py" \
  --d1-articles "$D1_ARTICLES_OBJECT" \
  --d1-articles-sha256 "$D1_ARTICLES_SHA256" \
  --annotations-dir "$ASSET_TREE/site/annotations" \
  --catalog "$ASSET_TREE/catalog/data/articles.jsonl" \
  --out-dir "$ASSET_TREE/site/out" \
  --out-w3c-dir "$ASSET_TREE/site/out_w3c"
"$WIKILEAN_PYTHON" "$ASSET_TREE/site/build_static_pages.py"
(cd "$ASSET_TREE/wiki" && "$WIKILEAN_BRAIN_NODE" --experimental-strip-types \
  scripts/build-mathlib-index.ts)
```

Use Python 3.12 and Node 22; all variables above are explicit absolute input/tool paths
except the lowercase hexadecimal digest. The D1 mode validates canonical article rows and
exact complete sidecars, then renders from the captured rows; ignored article HTML cannot
select which concepts get links. It does not independently authenticate the D1 acquisition.
The default exporter behavior remains available for existing workflows.

Copy exactly `404.html`, `robots.txt`, `concepts.html`, and `wikilean.ttl` from that tree's
`site/out` into `wiki/public`; copy `style.css`, `script.js`, and `review.css` from
`site/assets`, plus `wiki/assets/editor.js`, into `wiki/public/assets`. Keep the generated
`assets/mathlib-index.json`. Copy the three already verified index directories
(`decl-index`, `suffix-index`, `premise-index`) unchanged, with complete manifest-declared
shards/name chunks. Do not copy the generated `sitemap.xml`: the Worker serves it dynamically.
Do not invent article HTML or stage a synthetic Brain release. The unchanged `attest`
command checks every required asset and full index closure:

```bash
export PUBLIC_SOURCE_ROOT="$ASSET_TREE/wiki/public"
"$WIKILEAN_PYTHON" site/ops/brain_public_baseline.py attest \
  --source-public "$PUBLIC_SOURCE_ROOT" \
  > wiki/public-asset-source-attestation.json
```

Review and commit the exact inventory and retain the private input/program/output lineage.
The September 23 direct assembly recipe and measurements are retained under
`completion-20260923/public-asset-preparation` in the private migration store; the pinned
index recipe is under `public-index-preparation`. Its offline index generation uses the
retained declaration oracle and MathNetwork inputs, avoiding a fresh doc-gen download.

Any bootstrap Brain release used by the convenience path is excluded from the attestation
and is not the promotion candidate. After the exact inventory is reviewed and lands on
`main` as commit C, keep the
generated non-Brain public tree byte-for-byte unchanged—especially the timestamp-bearing
index manifests, and freeze it against C. Separately produce the actual candidate Brain
release through the P1B isolated shadow-build flow at the same commit C; do not use a
bootstrap release and do not rerun `npm run build:indexes`. Any non-Brain byte change
between `attest` and `freeze` is a hard mismatch, not a repair:

```bash
cd /Users/jackmccarthy/projects/WikiLean

AUTHORITY_COMMIT="$(git rev-parse HEAD)"
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_public_baseline.py freeze \
  --source-public "$PUBLIC_SOURCE_ROOT" \
  --store "$WIKILEAN_BRAIN_PUBLIC_BASELINE_STORE" \
  --repo-root "$PWD" \
  --authority-git-commit "$AUTHORITY_COMMIT"
```

The command prints the exact `baseline_id` and immutable root. Review and retain both.
The freezer excludes `brain.html` and `assets/brain/**`, rejects missing shell/index
assets, validates every manifest-declared shard/name chunk, and publishes only a canonical
manifest plus its complete read-only inventory. Confirm the shadow release result and the
baseline both name C as their authority commit. The September 23 continuation prepares the
first attestation for review; its presence on a work branch does not establish the final
main authority commit or complete P1B. Freeze reloads the inventory from the exact named
commit and rejects any mismatch with the retained source tree.

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

The retained P1B dry-run makes authenticated, read-only Wrangler status and history calls.
After `npm ci` in the promotion worktree, authenticate Wrangler in the operator shell and
verify the exact command families with the reviewed Node binary:

```bash
(
  set -e
  cd "$PROMOTION_WORKTREE/wiki"
  "$WIKILEAN_BRAIN_NODE" node_modules/wrangler/bin/wrangler.js \
    deployments status --config wrangler.jsonc --json >/dev/null
  "$WIKILEAN_BRAIN_NODE" node_modules/wrangler/bin/wrangler.js \
    deployments list --config wrangler.jsonc --json >/dev/null
  "$WIKILEAN_BRAIN_NODE" node_modules/wrangler/bin/wrangler.js \
    versions list --config wrangler.jsonc --json >/dev/null
)
```

Do not use `wrangler whoami` as this gate; Wrangler 4.120 can exit zero while reporting that
the shell is unauthenticated. If using `CLOUDFLARE_API_TOKEN`, export it locally only. Do
not place it in `nightly.local.env`, repository files, command arguments, logs, or review
messages.

After the shadow release, public baseline, and semantic comparison have been produced,
run the promoter's no-mutation path with retention enabled. The retained store must be an
absolute path outside every checkout, release, baseline, receipt, and temporary promotion
workspace:

```bash
# First activation only, while /assets/brain/current.json returns HTTP 404.
cd "$PROMOTION_WORKTREE"
bash site/ops/brain-promote-release.sh sha256:<candidate-release-hex> \
  --release-root /absolute/releases/<candidate-manifest-hex> \
  --public-baseline-id sha256:<public-baseline-hex> \
  --public-baseline-root /absolute/public-baselines/<public-baseline-hex> \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --dry-run \
  --retain-dry-run-store "$WIKILEAN_BRAIN_PROMOTER_DRY_RUN_STORE" \
  --allow-first-deploy-without-selector \
  > "$EVIDENCE_DIR/promoter-dry-run.json"
```

The dry-run flag acknowledges the observed selector topology so P1B can collect evidence;
it does not authorize deployment. Dry-run rejects `--first-deploy-approval`. After a
release-qualified selector exists, omit the exception flag; supplying it deliberately fails.

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
`wikilean.brain-activation-ci/v3` evidence retains the exact argv, working directory,
return code, tool-version probes, the canonical Node executable path, digest, and size, and
complete stdout/stderr for every successful gate. The freezer requires that exact Node
identity and version to equal the promoter dry-run's Worker toolchain.
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
  --release-manifest /absolute/releases/<candidate-manifest-hex>/release.json \
  --semantic-baseline-manifest /absolute/releases/<baseline-manifest-hex>/release.json \
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
rejects an identical candidate/baseline manifest and binds the reviewed prior release's
logical ID as an external trust anchor. When the promoter retained a live release, its
logical ID and exact manifest digest must both match that baseline. Verify the returned root
independently with both returned IDs and retain its bundle ID/root for review:

```bash
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain_activation_bundle.py verify \
  --bundle-root "$WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE/<bundle-hex>" \
  --expected-bundle-id sha256:<bundle-hex> \
  --expected-semantic-baseline-id sha256:<baseline-release-hex>
```

This entire section is P1B preparation only. `context`, the CI recorder, `freeze`, and
`verify` do not authorize or perform a production deployment. Actual evidence generation
remains blocked until Jack authorizes the host paths and evidence inputs above.

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

The retained P1B dry-run is the reviewed no-mutation preflight. Do not generate an ad hoc
replacement after freezing the activation bundle. If its evidence is stale, repeat P1B,
freeze a new bundle, and review the new IDs before activation.

Run the promoter from a separate clean checkout/worktree at the frozen release's recorded
authority commit. Point it at the explicit read-only release root/store produced by the
isolated shadow build. The current shadow reducer writes timestamp-bearing tracked outputs,
so building and promoting from one checkout would violate the clean-tree gate; do not solve
that by ignoring generated dirtiness.

After Jack approves the exact release, baseline, journal location, and exclusive window,
the mutating form is:

```bash
FIRST_DEPLOY_APPROVAL="Jack approved first deployment without a selector for release sha256:<64hex> in <exclusive-window>"

WIKILEAN_BRAIN_DEPLOY=1 bash site/ops/brain-promote-release.sh sha256:<64hex> \
  --release-root /absolute/release-store/<manifest-hex> \
  --public-baseline-id sha256:<baseline-hex> \
  --public-baseline-root /absolute/public-baselines/<baseline-hex> \
  --activation-bundle-id sha256:<activation-bundle-hex> \
  --activation-bundle-root \
    "$WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE/<activation-bundle-hex>" \
  --expected-semantic-baseline-id sha256:<baseline-release-hex> \
  --receipt-dir "$WIKILEAN_BRAIN_RECEIPT_DIR" \
  --execute \
  --allow-first-deploy-without-selector \
  --first-deploy-approval "$FIRST_DEPLOY_APPROVAL" \
  --approval-note "Jack approved release <id>, baseline <id>, and this window"
```

For later promotions with an existing release-qualified selector, omit both first-deploy
options.

Execution re-verifies the exact activation bundle and its retained dry-run artifact,
then uploads those reviewed public, Worker, and Wrangler bytes. It does not regenerate
deployment inputs. The bundle ID, root, semantic baseline, and retained artifact identity
are recorded in the durable intent and deployment event.

If production has no release-qualified selector, dry-run requires the exception flag but
forbids an approval string. Execution requires the same exception flag plus Jack's new P1C
approval for the exact release and window. That approval is added to the reviewed intent
and fsynced before Wrangler can mutate production. TLS, DNS, and timeout failures are never
an acceptable substitute for the explicit flag.

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

Run the same release-qualified canary manually with both the logical release ID and
the bare SHA-256 digest of the exact `release.json` bytes:

```bash
cd /Users/jackmccarthy/projects/WikiLean
"${WIKILEAN_PYTHON:-.venv/bin/python3}" site/ops/brain-canary.py \
  --base-url https://wikilean.jackmccarthy.org \
  --expected-release-id sha256:<release-hex> \
  --expected-manifest-sha256 <manifest-hex> \
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

The canary requires all of the following to agree on the expected logical release and
exact manifest:

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
  --expected-manifest-sha256 <predeploy-manifest-hex> \
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
