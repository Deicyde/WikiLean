# Brain release runbook

This runbook covers Phase 1 static Brain releases for `wikilean`. Shadow release
construction is ready, but production activation is blocked until roadmap milestone P1A
can promote an exact reviewed release without rebuilding. Keep
`WIKILEAN_BRAIN_DEPLOY=0`.

## Safety model

After P1A lands, an exact frozen release is eligible only after all existing data/page
gates and these release gates pass:

1. `brain/tools/build_release.py` freezes a content-addressed release in `site/out/brain-releases/<64hex>/`.
2. `brain/tools/verify_release.py` independently verifies the frozen bytes and attestations.
   The current `brain-current-v1` profile requires the WLBN SQLite schema v2 and
   path-specific media/logical formats; legacy schema-v1 indexes are not publishable.
3. `wiki/scripts/build-public.ts` stages the new release, the exact prior production
   release when one exists, byte-identical mutable aliases, and the Brain page
   copied and digest-checked from that same frozen release. It never sources the
   release-coupled page from mutable `site/out`.
4. Worker typecheck and unit tests run against the staged bytes.
5. Because `wiki/public` is generated and gitignored, the exact frozen release is
   transactionally restaged once more after all slow checks.
6. After that last mutation, the script rechecks `main`, the frozen authority
   commit, merge/rebase state, and the exact release-affecting dirty set, then
   records production as Worker status A → exact selector → Worker status A.
7. Wrangler runs once with `--strict`, a release tag, and a release message. The
   emitted candidate Worker version B is polled for 100% traffic before and after
   the canary. Once Wrangler is invoked, the release-content canary still runs if
   the command returns nonzero or version parsing/control-plane polling fails;
   rollback is disabled unless candidate ownership was proven.
8. `site/ops/brain-canary.py` waits for selector, manifest, required view assets,
   cell manifest/shard, `/brain`, `/brain.html`, REST API/cursor, MCP, and aliases to agree.

The nightly script derives the repository root from its own physical location. Do not copy
`brain-nightly.sh` outside the checkout and invoke that copy. Until P1A is complete, these
gates describe the target promotion protocol rather than authorization to deploy.

## Activation prerequisites

Before running even a shadow build, create the gitignored
`site/ops/nightly.local.env` with a readable, read-only Mathlib tree and Python
3.12+ (or set the same variables in the invoking environment):

```bash
cp site/ops/nightly.local.env.example site/ops/nightly.local.env
```

Then replace the example placeholders in that file. For a one-off interactive
run, the equivalent environment is:

```bash
export BRAIN_MATHLIB_CHECKOUT=/absolute/path/to/mathlib4/Mathlib
export WIKILEAN_PYTHON=/absolute/path/to/python3.12
```

The job fails closed before fold/build if the Mathlib tree is unset or missing.
The optional proposal agents use a separate interpreter
(`WIKILEAN_BRAIN_AGENT_PYTHON`, default `catalog/.venv/bin/python3`) and are
skipped with an explicit warning if that environment is absent. Verify all configured
paths from the same launch context that will run the job; missing paths fail closed.

## Shadow release

Deployment remains disabled unless explicitly enabled:

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
- after a deployment or rollback canary succeeds, `site/out/brain-canary-result.json`:
  the converged release, attempts, duration, request count, checked response
  bytes, and process maximum RSS.

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

## Deploy

> **Activation is currently blocked.** The nightly deploy flag rebuilds before it
> deploys, while `generated_at` is still nondeterministic, so it cannot yet promote the
> exact frozen release that was reviewed in a prior shadow run. Do not enable
> `WIKILEAN_BRAIN_DEPLOY` until roadmap milestone P1A adds exact-release promotion and a
> canary TLS preflight.

The legacy deploy-enabled nightly is not an approved activation interface. It rebuilds
before deploying and may therefore publish a release other than the reviewed candidate.
Keep the nightly in shadow mode.

The planned P1A interface is shown for review only; it is not available until that
milestone is implemented and verified:

```bash
cd /Users/jackmccarthy/projects/WikiLean
# Run only after Jack approves this exact sha256:<64hex> and deployment window.
bash site/ops/brain-promote-release.sh sha256:<64hex> \
  --release-root /absolute/path/to/brain-releases/<64hex>
```

Run the promoter from a separate clean checkout/worktree at the frozen release's recorded
authority commit. Point it at the explicit read-only release root/store produced by the
isolated shadow build. The current shadow reducer writes timestamp-bearing tracked outputs,
so building and promoting from one checkout would violate the clean-tree gate; do not solve
that by ignoring generated dirtiness.

If production has no release-qualified selector, the promoter must fail unless the
operator also supplies `--allow-first-deploy-without-selector`. That exception requires
Jack's approval for the exact window and must be written into the deployment intent record.
TLS, DNS, and timeout failures are never an acceptable substitute for the explicit flag.

Every promotion uses a crash-safe append-only event journal. Before any mutating Wrangler
call, the promoter fsyncs an intent record containing the attempt ID, requested/prior
release IDs, authority commit, and predeploy Worker version. It then appends immutable
deploy-result, canary, rollback, and final-state records linked to that attempt. A derived
summary may be generated but never replaces or mutates the evidence records.

Gitignored `site/out` is not a durable journal sink. Set
`WIKILEAN_BRAIN_RECEIPT_DIR` to an absolute directory outside the checkout; the promoter
must fail if it is unset or unwritable. Never garbage-collect these records automatically,
include the directory in host backups, and attach each event-chain hash to the rollout
review or incident record. On startup, the promoter refuses another mutation until every
incomplete attempt has been reconciled against live Wrangler and selector state.

The P1A `--dry-run` may call read-only Wrangler status/history commands to validate the
account and record the current sole 100% Worker version. It must never invoke deploy or
rollback.

Before staging, the script fetches the production `/assets/brain/current.json`. If production names a prior qualified release, its exact frozen directory must still exist and independently verify locally. This prevents a deployment-disabled shadow run from accidentally becoming the retained `previous` release. Immediately before the deploy, the selector must still be byte-identical and the Worker version must remain stable across the status/selector/status sandwich.

The deploy path invokes exactly one strict, tagged `npm run deploy` and parses
the candidate version from Wrangler's `Current Version ID`. Once Wrangler has
been invoked, the release-qualified canary always runs, even if the command
returns nonzero or the control-plane response cannot establish candidate
ownership: the remote write may already have landed. Automatic rollback remains
disabled unless the parsed candidate version was proven to own 100% of traffic.
Any failure before the Wrangler invocation leaves production unchanged;
deployment uncertainty or canary failure returns nonzero.

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
  --timeout 300 \
  --interval 5
```

Every request carries a unique cache-busting query parameter and `Cache-Control: no-cache`.
Each response is capped at 32 MiB. Success prints versioned JSON including
`attempts`, measured `convergence_seconds`, request/byte counts, and maximum RSS;
the shadow nightly stores that result under `site/out`, while the promoter appends it to
the durable attempt journal. The initial operational target is
convergence within five minutes; this is a target, not a measured rollback SLO.

The canary requires all of the following to agree on the expected release:

- Strict `current.json` selector and immutable `release.json` identity.
- Cell manifest, a deterministic manifest-declared shard, labels, supercells,
  explorer, and frontier graph.
- Sampled immutable files matching the byte length and SHA-256 declared by `release.json`.
- `/brain` and `/brain.html` both loading through the release selector, matching
  the frozen manifest byte-for-byte, and matching each other.
- A representative Brain API response with matching `release_id`, snapshot metadata, and a new opaque cursor that advances.
- A representative `POST /mcp` `brain_filter` call with the expected JSON-RPC
  envelope, no tool error, and a payload naming the same release.
- Mutable `cells/`, `sources.json`, and `xref_index.json` samples byte-equal to their immutable current-release counterparts.

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

Automatic rollback is deliberately disabled by default:

```bash
export WIKILEAN_BRAIN_AUTO_ROLLBACK=0
```

Wrangler 4.120 has no rollback compare-and-swap operation. The safer normal
response to a failed canary is to inspect state and perform the manual rollback
below. `WIKILEAN_BRAIN_AUTO_ROLLBACK=1` exists only for an explicitly exclusive
deployment window; it sandwiches the live candidate selector between two checks
that candidate version B still owns 100% traffic, but a residual race remains.

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

The P1A promoter must leave a failed candidate in place when automatic rollback is off,
append the observed failure state, return nonzero, and print the manual recovery path.
This avoids blindly overwriting an unrelated newer deployment.

When the explicit automatic-rollback opt-in is active, the promoter first requires
candidate version B → candidate selector → candidate version B, then runs the
exact rollback command above, confirms the recorded version A owns 100% traffic,
and polls for the prior selector, assets, API, page, and aliases. Because Wrangler
does not expose a CAS, this mode is still restricted to an exclusive deploy window.

If the prior Worker version is absent or ambiguous in the durable intent record, the
promoter does not guess. Inspect `npx --no-install wrangler deployments list --json` and
`versions list --json`, reconcile the intended prior Worker/release pair with the selector
and release records, then run `npx --no-install wrangler rollback <version-id> --yes` and
canary the matching prior Brain release.

After a rollback, roll forward only by restoring the recorded candidate Worker version or
by promoting the same exact frozen release through the approved promoter. Do not rerun the
nightly for roll-forward: it rebuilds and may introduce an unreviewed release.

If the rollback command succeeds but the prior release does not converge, keep the run failed. Reinspect deployment status, fetch `/assets/brain/current.json` with `Cache-Control: no-cache`, confirm the prior immutable namespace exists, and rerun the canary. Escalate rather than repeatedly deploying or deleting release directories.
