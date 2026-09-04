# WikiLean Development Roadmap

> **Living document.** Produced 2026-06-10 from a full architecture audit (8 investigation
> agents + 2 adversarial verifiers, cross-checked against the codebase). Update statuses
> as work lands; do not re-litigate the binding decisions below without new evidence.

## End goal (verbatim from Jack)

A complete interface mapping Wikipedia statements to their formal implementation in
Mathlib (and eventually CSLib, PhysLib, etc.). Primarily AI-moderated via three routine
operations: **(1)** generate annotations for new articles, **(2)** review previous
articles and correct mistakes, **(3)** update articles as Wikipedia's content changes.
Anyone can donate compute ("token donations": run the script locally on their own
Claude account/API key). Humans can quickly correct errors on the site. The project is
an **experiment in human+AI database moderation** — collecting data about that
interaction is a first-class goal. Clean UI is key.

## The central finding

The human+AI loop is currently **severed in four places**:

1. **AI moderation never sees human edits.** Pipeline reads `site/annotations/*.json`
   (disk); humans write only to D1. No D1→disk path exists, so `_preserve_human`
   (batch_annotate.py) has never run against a real human edit.
2. **Human-edited articles are frozen out of AI review forever** —
   `seed-refresh.ts` permanently skips slugs with a `user_id IS NOT NULL` revision.
3. **The review selector self-terminates** — `find_old_articles()` only selects
   pre-v3 articles; nothing records "last reviewed at."
4. **Wikipedia-update tracking is 0% built** — revids pinned forever at four layers
   (local HTML cache, WP_HTML KV, D1 COALESCE, save handler); nothing detects drift.

## Binding architecture decisions (verifier-adjudicated)

These resolved real conflicts between competing proposals. Build each thing **once**:

- **D1-direct pipeline, not disk-canonical.** The pipeline reads via
  `GET /api/article/:slug.json` and writes via bearer-authenticated
  `POST /api/article/:slug` with `expected_version`. Disk files demote to
  cache/backup artifacts. `seed-delta`/`seed-refresh` retire to legacy migration tools.
- **One optimistic-concurrency mechanism.** Field name `base_version`; server returns
  409 + current `{version, annotations}` on mismatch. Same contract for editor and
  pipeline. (Four investigators proposed this independently under different names.)
- **One work table** (`moderation_state`: last_reviewed_at, last_reviewed_version,
  wp_latest_revid, wp_drifted, flag_count, state, proposal) + `GET /api/work` with the
  priority policy in one ORDER BY: **flagged > drifted > human-edited-since-review >
  never-moderated > oldest-reviewed**. (REVISED 2026-06-12 with evidence, per the
  review pass: never-moderated sorts before stale-reviewed — every article already
  carries one pipeline annotation pass, so first-moderation coverage beats re-review;
  the original wording had "new" last. Also: "flagged" means flagged-SINCE-last-review,
  or open flags livelock the queue front; moved/deleted/needs_human states are
  excluded from selection entirely.) No separate `article_updates` table; the
  claim-based `jobs` table is deferred behind the first-real-donor trigger and
  replaces /api/work's internals, not the runner.
- **One runner script** — `site/moderate.py` with subcommands `new | review | wp-update`,
  flags `--auth subscription|api-key` and `--mode trusted|contributor`. No separate
  contrib_runner.py.
- **One bearer scheme.** Start with a single `PIPELINE_TOKEN` Worker secret checked in
  `getUser` (single auth seam, auth.ts). Graduate to an `api_tokens` table with a
  `scope` column when a second token-holder exists.
- **One status enum**, defined once and imported by wrap.ts, the save validator, and
  any future gauntlet: `{formalized, partial, not_formalized}` now; `rejected`
  (human-deletion tombstones) is added **in the same patch** that ships tombstones +
  the wrap-skip, never separately.
- **One revid policy (the most important invariant in the system):**
  `articles.revid` advances **only atomically with a re-anchored annotations payload**.
  Stale-but-consistent is the product guarantee. `latest_revid` /
  `last_upstream_check` columns may be written freely (they never bump `version` and
  the staleness UI is injected per-request, never baked into the cached base page).
- **One Mathlib decl index artifact** (~300k names from doc-gen4 declaration-data):
  sharded static files consumed by editor autocomplete AND server-side
  hallucinated-decl validation. The current mathlib-index.json (4,598 self-bootstrapped
  decls) is an autocomplete boost tier, not an oracle.
- **Token donations: never take custody of keys.** Local runner with the donor's own
  key/subscription is v1. The safe "donate a key" is a GitHub Actions fork template
  (key lives in the donor's fork secrets). Claude subscription credentials are
  categorically un-donatable. No server-side key vault, ever, unless demand forces a
  re-evaluation priced as a major ongoing security commitment.
- **Attribution lives at revision level** (`revisions.kind` + `meta` JSON), never in
  the annotation-level provenance enum — `_preserve_human` and the PRIORITY ladder do
  exact string matching on `human`/`ai-moderated`/`ai`.
- **Human-at-boundaries decision policy (RATIFIED 2026-08-06, supersedes the
  human-gate on intra-site proposals).** Jack, verbatim: "I think humans need to
  come in only for adding cross-refs between sites where one site only allows
  human edits. e.g. Mathlib, Wikipedia." Confirmed via explicit prompt, both
  parts: (1) AI decides ALL pending proposals in WikiLean's own D1 — including
  those targeting provenance:'human' annotations, which flip to 'ai-moderated'
  when AI-changed (attribution is NEVER laundered: AI decisions record
  actorType 'ai', distinct revision kind, never mint 'human'); (2) the nightly
  auto-decides unattended (WIKILEAN_AUTO_DECIDE=1) via a deterministic resolver.
  Humans remain the gate ONLY where WikiLean pushes into human-edited systems
  (mathlib4 PRs, Wikidata submissions, Wikipedia). findLostHuman's 422 on bot
  SAVES survives as an anti-clobber guard, not a policy gate — deliberate
  change flows through the decide path. Companion policy, same date:
  proof_wanted stubs badge 'partial' ('formalized' alone is an overclaim).
  BUILT 2026-08-07 (bearer decide path + GET /api/proposals +
  site/resolve_proposals.py + nightly WIKILEAN_AUTO_DECIDE + stats v5).
  *Implementation note on the letter of (1):* annotation_events.actor_type is
  constrained by migration 0005 to {'human','pipeline'} — AI decisions record
  the schema's existing AI-actor value **'pipeline'** (the same label every
  bot save already carries), with the channel separated by revision kind
  `proposal-decided-ai` + proposals.decided_by. Recording literally 'ai'
  would need a D1 table rebuild to relabel a synonym; flagged to Jack
  2026-08-07 — say the word if the literal enum value matters.
- **Schema v4 (multi-library `formalizations[]`) is deferred** and must be **bundled
  into the annotation-ID backfill migration** (one corpus rewrite, not two). Written
  trigger: CSLib covers a typical undergrad algorithms course. Wikidata property
  proposal stays Mathlib-only (one external-ID property per library is the Wikidata
  convention).

---

## Brain authority, storage, and release

The binding design is [`docs/BRAIN-ARCHITECTURE.plan.md`](BRAIN-ARCHITECTURE.plan.md).
The Brain keeps separate representations for separate jobs: reviewed Git authority and
sealed source transitions; deterministic JSONL compatibility exports; an immutable,
generated SQLite projection for complete offline queries; static release-qualified
cell shards for bounded public reads; and a release-pinned D1 overlay for immediate
community edits. SQLite, D1, static assets, and any future PostgreSQL projection are
never independent semantic writers.

- [x] **Indexed local SQLite projection** — landed 2026-08-28. Atomic rebuild,
  JSONL parity/freshness checks, cells, organ ownership, synapses, and indexed
  endpoint traversal are implemented; JSONL remains reviewable interchange.
- [x] **Phase 0 contracts** — canonical encoding/logical roots, reducer-input
  inventory, source/offline-pack schemas, release/attestation schemas, offline
  verification runner, and semantic snapshot diff landed 2026-08-29.
- [ ] **Finish Phase 0 reproducibility (P0-R below)** — seal real source manifests/objects,
  eliminate ambient checkout/mtime/cache identity from authoritative reduction,
  and prove two network-disabled clean-room builds produce the same logical roots.
- [x] **Phase 1 immutable-release implementation** — landed locally 2026-08-31.
  Graph, SQLite, cells, frontier, shards, traces, xrefs, and the Brain page now
  freeze under one release ID; Worker reads and cursors are release-qualified;
  independent verification checks full closure; public staging retains current
  and previous immutable namespaces plus byte-identical compatibility aliases;
  release-aware canaries are wired. Deployment remains explicitly gated, and
  rollback is manual-only because Wrangler exposes no compare-and-swap primitive.
- [ ] **Phase 1 production activation and rollback drill (P1A–P1C below)** — review one shadow
  result, enable one production release from a clean `main`, measure CDN/isolate
  convergence, deliberately exercise the documented rollback path, and record
  the observed recovery time before claiming a rollback SLO.
- [x] **SQLite v2 hardening** — landed 2026-08-31. Stable base/projection/release
  identities, distinct logical and raw-file digests, pinned-source/race checks,
  durable atomic publication, persisted planner statistics, indexed bounded
  endpoint probes, streamed release verification, and machine-readable
  performance/resource metrics are implemented.
- [ ] **Phase 2 Git authority (P2A–P2D below)** — immutable curated changesets and reviewed source
  transitions, first-parent/CAS validation, semantic migrations, deterministic
  replay, and a reviewed genesis import. Do not expand deterministic source facts
  into one Git operation per edge.
- [ ] **Phase 3 release-pinned D1 overlay (P3A–P3D below)** — append-only operations and decisions,
  stable assertion IDs/tombstones, monotonic sequences, deterministic rebase,
  exactly-once promotion receipts, compatible rollback generations, and proven
  backup RPO/RTO.
- [ ] **Phase 4 generated-artifact retirement from Git** — publish generated
  JSONL/SQLite/shards as immutable release artifacts while retaining reproducible
  compatibility-export commands.

### Current Brain execution queue (updated 2026-09-02)

P1A's exact-release promoter and P1B's evidence-recorder/bundle tooling are implemented and
remain production-inactive. The current commit intentionally has no
`wiki/public-asset-source-attestation.json`, so public baseline freeze/verify fails closed
until the first complete attestation is created and reviewed. Actual P1B evidence generation
is blocked on Jack merging P1A onto `main` and authorizing the launch-context Mathlib and
Git/Node/npm/Python paths. The next operational ticket is to produce and review one complete activation
evidence bundle, including the immutable non-Brain public baseline, without changing
production. In parallel, P0-R remains the main architecture workstream. Its first
contract/input-closure tranche is implemented: versioned v2 source-manifest,
offline-pack, reducer-input-inventory, and full-offline-replay build-attestation
contracts describe the current seven-stage post-acquisition build DAG, explicit logical
roots, and required-versus-absent inputs. All seven stages now consume the immutable runtime
context: base graph, top-level shards, cells, SQLite-with-cells, frontier, cell shards, and
the input-free Brain page. Bound normalized input and reducer bytes are copied into atomically
published read-only views; builders use exact context bindings, predecessor outputs, source
pins, reducer configuration, and the pack-derived generation identity rather than caller paths
or environment configuration. Shared stage I/O provides deterministic private scratch,
durable atomic no-replace publication for files and trees, rollback of partial multi-output
publication, and cross-filesystem refusal.

`run_offline.py` now accepts v2 packs, prepares a fresh workspace, and delegates to the
single fail-closed `run_replay_v2.py` executor. The executor re-verifies the sealed input and
reducer closures, runs every inventory stage in order (including independent leaves), requires
a supported OS isolation boundary with networking denied, and rejects undeclared output,
scratch residue, or predecessor mutation. This is not yet a production reproducibility claim:
there is no real full-corpus pack, the Python/NumPy/SQLite runtime is not pinned, and the
generated sandbox policies have not yet been exercised on clean supported hosts. Linux no
longer bind-mounts the host root: reducers see the exact prepared workspace, an empty temp
directory, kernel-local `/proc` and `/dev`, and the selected runtime roots. Darwin grants
reads only to the exact workspace, selected runtime roots, and Apple's standard system
runtime profile; process forking and networking are denied. Current release creation
therefore continues to use the v1 compatibility authority. P2A is a
safe third, shadow-only workstream, but P2B and
later wait for P0-R's source/build contracts. Do not start a Phase 3 D1 schema cutover
until all Phase 2 contracts and the reviewed genesis are complete.

#### P1A — exact frozen-release promotion `[IMPLEMENTED 2026-09-02; NOT ACTIVATED]`

- [x] Add `site/ops/brain-promote-release.sh <release-id> --release-root <absolute-path>`,
  where `<release-id>` is the complete `sha256:<64hex>` identity. It must never run
  ingest or reduction: verify the named immutable release, require its recorded authority
  commit to equal a clean `main` HEAD, retain the exact live release, stage the named
  current/previous pair, run Worker checks, fence production as
  status A → selector A → status A, deploy once, and canary the requested release ID.
  Run promotion from a separate clean checkout/worktree at the release's authority commit
  and read the candidate through an explicit read-only release-root/store path produced by
  the isolated build. Do not weaken the clean-tree gate by ignoring generated dirtiness.
  Require a separately frozen, content-addressed non-Brain public-asset baseline, and deploy
  only the sealed dry-run Worker bundle plus the sealed external asset tree. Provide a
  no-mutation `--dry-run` that executes all local checks, live transport preflight,
  read-only Wrangler identity/status/history calls, and Wrangler's local no-upload compile
  validation, but never invokes a mutating deploy or rollback.
- [x] Write a crash-safe append-only deployment journal. Before any mutating Wrangler call,
  fsync an intent record containing the attempt ID, requested/prior release IDs, authority
  commit, and predeploy Worker version. Append immutable invocation, deploy-result, canary,
  reconciliation, and final-state records linked to that attempt; a derived summary may be generated but
  never overwrites evidence.
  Require an absolute `WIKILEAN_BRAIN_RECEIPT_DIR` outside the checkout and gitignored
  `site/out`; fail if it is unset or unwritable, never garbage-collect it automatically,
  include it in host backups, and attach the event-chain hash to the rollout review. On
  startup, refuse another mutation until every incomplete attempt is reconciled against
  live Wrangler and selector state.
- [x] Fix Python TLS trust for the canary using a maintained CA source (`truststore` or
  `certifi`); never disable certificate verification. Run a transport preflight with the
  same opener before invoking Wrangler. HTTP 200 proves the normal path. A missing selector
  is accepted only with `--allow-first-deploy-without-selector`, Jack's approval for that
  exact window, and the exception recorded in the intent journal. TLS, DNS, and timeout failures
  fail closed.
- [x] Add failure-injection tests for exact-ID mismatch, dirty/wrong Git authority,
  unattested public bytes, incomplete index families, selector/version races, Wrangler
  returning nonzero after a possible remote commit, interrupted child-process cleanup,
  malformed or unwritable journals, crash/SIGKILL after Wrangler, first-deploy flag misuse,
  canary timeout, and uncertain-command reconciliation.

**Done when:** a reviewed shadow release can be promoted without rebuilding, the requested,
staged, deployed, and canaried release IDs are identical, and every attempt either has a
complete immutable event chain or is detected and reconciled before any later mutation.
Keep deployment disabled through P1B and until the explicit P1C approval window. Automatic
rollback is not implemented; recovery is manual and independently approved. The legacy
`WIKILEAN_BRAIN_DEPLOY=1` nightly path and its deployment code are deleted because it rebuilt
before deployment.

#### P1B — activation evidence bundle `[TOOLING IMPLEMENTED; PREP; NO PRODUCTION]`

- [x] Implement `site/ops/brain_activation_ci.py`. It runs the exact `npm ci`,
  `npm run test:ci`, and `PYTHON=<selected> ./scripts/ci-python.sh` gates in the clean
  promotion checkout, checks Node 22/Python 3.12 and Git authority before and after,
  strips inherited credentials and Git overrides, and emits canonical
  `wikilean.brain-activation-ci/v2` evidence with complete command output.
- [x] Implement `site/ops/brain_activation_bundle.py context|freeze|verify`. The immutable
  bundle contains exactly 11 evidence files, validates their identities and external
  worktree/artifact roots before freezing, generates fresh CI evidence in-process using
  explicit absolute Git/Node/npm/Python paths with caller `PATH` discarded, and freezes a
  fresh fixed-setting SQLite measurement. It remains independently verifiable after the
  mutable build context is gone when retained with its required content-addressed promoter
  companion root. Its semantic comparison requires an externally supplied prior
  release ID, rejects candidate self-comparison, and requires
  `wikilean.semantic-diff/v2` coverage of all seven compatibility paths: nodes, both edge
  streams, cells, synapses, frontier rows, and the frontier graph.
- [x] Make promoter dry-run evidence durable and inspectable. With
  `--retain-dry-run-store`, the no-mutation path atomically freezes the exact sealed public
  tree, Worker bundle, Wrangler config, and raw selector/status/history bodies in an
  external content-addressed read-only root; the activation freezer re-verifies those
  bytes before and immediately before publication. Verification also proves the complete
  retained non-Brain public file closure equals the immutable baseline and refuses to pass
  if this companion root is unavailable.
- [ ] **Jack prerequisite:** review and merge the final Phase 1/P1A pull request onto a
  clean `main`, and authorize the read-only Mathlib checkout plus Git/Node/npm/Python
  executable paths used by the launch job.
- [ ] Provision and verify those paths plus gitignored `site/ops/nightly.local.env` in the
  same launch context used by the job, including the external retained-dry-run and
  activation-bundle stores and the approved absolute Git/Node/npm/Python executables. Keep
  agents and deploy disabled.
- [ ] At the reviewed tip, run activation-bundle `freeze` from the clean promotion
  checkout with the approved Git, Node, npm, and Python executables. The freezer itself
  executes and retains the exact Worker and Python CI evidence; standalone or hand-written
  summaries are not freeze inputs.
- [ ] Run the shadow nightly in an isolated build worktree/output context so its
  timestamp-bearing generated files cannot dirty the clean promotion checkout.
- [ ] Build all non-Brain Worker assets once (including declaration/suffix/premise indexes
  and shell files generated by `site/export_wikidata_rdf.py` followed by
  `site/build_static_pages.py`), render `brain_public_baseline.py attest` into the fixed
  `wiki/public-asset-source-attestation.json`, review and commit that exact inventory, then
  freeze the unchanged ignored public tree against the resulting authority commit. Never
  rebuild the timestamp-bearing indexes between attestation and freeze. Record the exact
  baseline ID/root beside the exact Brain release ID/root, and verify the promoter's
  baseline-aware canary samples every required family.
- [ ] Freeze or identify a complete trusted semantic pre-activation comparison bundle, and
  store the release/store/public metrics plus JSON from this command:

  ```bash
  "${WIKILEAN_PYTHON:-.venv/bin/python3}" brain/tools/semantic_diff.py \
    --from <baseline-release-root-or-manifest> \
    --to <candidate-release-root-or-manifest> \
    > <review-bundle>/semantic-diff.json
  ```

  The v2 semantic report must completely compare the seven compatibility paths:
  `nodes.jsonl`, `edges.jsonl`, `edges_links.jsonl`, `cells.jsonl`, `synapses.jsonl`,
  `frontier.jsonl`, and `frontier_graph.json`. The activation bundle separately verifies
  the complete candidate/baseline release manifests, including SQLite and release-coupled
  static artifacts. A sealed `brain/data` comparison is partial supplemental evidence only;
  it cannot satisfy activation-bundle freeze or P0-R semantic parity.
- [ ] Run the exact promoter through local verification and transport dry-run with
  `--retain-dry-run-store`, then review its proposed intent and retain the referenced
  content-addressed execution-artifact root without invoking a mutating Wrangler command.
- [ ] Generate the verified two-worktree context, assemble all 11 evidence files, freeze
  them under `WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE`, and independently run
  `brain_activation_bundle.py verify` with both the expected bundle ID and reviewed prior
  release ID on the resulting immutable bundle.

**Done when:** exact release A and its complete two-root review set (activation bundle plus
the referenced retained promoter artifacts) are ready, all automated checks pass, and
production has not changed.

#### P1C — production activation and rollback drill `[JACK GATE]`

- [ ] Jack approves the exact release A ID, exclusive deployment window, journal location,
  and (only if applicable) the first-deploy missing-selector exception.
- [ ] Promote release A through the manual-only recovery path and record end-to-end canary
  convergence. A first compatibility deployment does not by itself prove rollback.
- [ ] Build and review exact release B. Jack separately approves B's exact ID and promotion
  window before it is promoted.
- [ ] Jack separately approves the rollback action/window; manually roll B back to the
  Worker A version/release pair recorded before B, canary A, and record convergence.
- [ ] Jack chooses and approves the final A-or-B state before any final mutation. Roll
  forward only with the already-recorded
  Worker B version or the exact frozen release B; never rerun the nightly and accidentally
  introduce an unreviewed release C.
- [ ] Keep unattended deployment off until P0-R is complete and the drill evidence has
  been reviewed. Installing the LaunchAgent in shadow-only mode is safe after host setup.

**Done when:** two release-qualified versions have been exercised in production, rollback
and roll-forward both converge across selector, page, REST, MCP, cursor, and alias checks,
and the measured recovery time is recorded. Production changes always require Jack's
explicit approval.

#### P0-R — sealed inputs and deterministic clean-room replay `[PRIMARY ENGINEERING]`

- [x] **Evolve the contracts explicitly.** Added new, versioned source-manifest,
  offline-pack, reducer-input-inventory, and build-attestation contracts rather than
  loosening v1 in place. The v2 contracts represent raw plus normalized source objects,
  curated Git trees, the complete multi-file reducer DAG, `offline_pack_id`,
  `source_set_root`, and required-versus-absent optional inputs. The v2 document shapes are
  validation-ready and the fixture replay path is implemented. No real full-corpus pack or
  authoritative replay is claimed, and current release creation continues to use the v1
  compatibility contracts until environment pinning, clean-room dual-build evidence, and
  independent verification land.
- [x] **Repair declared input-inventory closure.** Replaced the ineffective Python brace glob
  `catalog/data/external/*_{pages,links}.jsonl` with explicit page/link patterns; added
  consumed `brain/data/discovery_rejected.jsonl`, optional
  `catalog/data/tauceti_links.jsonl`, and reducer code `brain/layout.py`; represented the
  external Mathlib tree explicitly; distinguished required inputs from deliberately absent
  optional inputs.
- [ ] **Separate acquisition from replay.** Network-enabled acquisition, including
  Wikidata checks used by `fold_proposals.py`, ends by sealing normalized/folded objects.
  The authoritative full-DAG replay begins after that boundary and performs no network or
  live D1 reads.
- [x] **Introduce an explicit build context.** Add one full-DAG replay entry point with
  separate read-only input and writable output roots. Route builders through explicit
  file lists, source pins, generation identity, and versioned reducer configuration
  instead of repository globals, live `BRAIN_*` environment lookups, or discovered glob
  members.
  - [x] Define a strict immutable runtime context with relocation-independent generation
    identity, exact input bindings/source pins, versioned reducer knobs, disjoint physical
    roots, and stage-scoped output/scratch accessors.
  - [x] Assign every DAG output to one non-overlapping stage and give the base-graph stage
    a JSONL-only mode so SQLite has one owner.
  - [x] Materialize bound normalized pack objects and reducer files into read-only input/code
    views, then generate the runtime context without trusting caller paths or environment.
    Preparation uses copy-only private staging, exact modes, fsync, case/Unicode and ancestry
    checks, and atomic no-replace publication; it never executes reducer code.
  - [x] Route all seven stages through the context and add the single fail-closed replay
    entry point. `needs` records direct generated-byte dependencies; the runner executes
    every stage in inventory order, including independent leaves.
    - [x] Route `top-level-shards` through exact base-graph and sealed source inputs.
    - [x] Route `sqlite-with-cells` through all five exact JSONL predecessor outputs.
    - [x] Route the input-free `brain-page` stage without claiming a false shard dependency.
    - [x] Add shared deterministic-mode, durable, atomic no-replace file publication with
      pair rollback and stage-owned scratch cleanup.
    - [x] Route `base-graph` through an explicit adapter over `build_common.py` inputs and
      eliminate its repository globals, environment reads, globs, and mtime discovery.
    - [x] Route `cells`, `frontier`, and `cell-shards` through exact context inputs and
      predecessor outputs; generated inputs that replay requires must fail closed.
    - [x] Add the single replay runner, make `run_offline.py` dispatch verified v2 packs to
      it, and require supported OS isolation with network disabled and writes confined to
      output/scratch on the host (Linux also gets an isolated ephemeral `/tmp`). The runner
      verifies the sealed input and reducer closures before
      execution, rechecks reducer bytes after every stage, and enforces output ownership and
      predecessor immutability. Its CLI refuses execution unless the original Python process
      was launched with `-I`, so caller `PYTHONPATH`, user-site, and startup hooks cannot run
      before the replay boundary is established.
- [ ] **Prove the narrowed execution namespace on supported clean hosts.** The generated
  Linux boundary now replaces the former host-root bind with only the exact prepared
  workspace, selected Python runtime roots, isolated `/tmp`, and namespace-local `/proc`
  and `/dev`; Darwin grants only the workspace, selected runtime roots, and Apple's standard
  system runtime reads, while denying process forks and networking. Add real kernel-level
  integration tests for both policies, then bind them to the pinned runtime identity before
  treating this as clean-room evidence. Completed predecessor outputs are still protected by
  cryptographic post-stage checks rather than stage-specific read-only mounts.
  - [x] Add a kernel-level hostile-probe suite over the production sandbox command, with
    explicit local skip reporting and fail-closed Darwin/Linux clean-host modes.
  - [ ] Run and retain strict evidence on both supported clean-host policies under their
    pinned environment identities.
- [x] **Remove ambient identity from the v2 replay path.** Context-mode reducers use exact
  source-manifest pins and the pack-derived generation ID for logical `generated_at` values;
  they do not consult input mtimes or the wall clock. Legacy live-build entry points retain
  their compatibility timestamps and are not authoritative replay evidence. Observation and
  build times remain outside v2 logical roots and snapshot IDs.
- [ ] **Pin the execution environment.** Record an exact Python, NumPy, SQLite, dependency
  lock, locale, and runner/container identity. A floating `numpy>=1.24` environment is not
  sufficient evidence for release-ID reproducibility.
  - [x] Define and enforce canonical `execution-environment/v1` descriptors in
    offline-pack/v2, with separate non-authoritative development-host and digest-pinned
    authoritative OCI profiles.
  - [x] Materialize the exact verified descriptor as a copy-only read-only workspace file;
    make the runner reject missing, altered, linked, writable, noncanonical, invalid, or
    reducer-commit-mismatched descriptors before sandbox selection or stage execution.
  - [ ] Fail closed when the live Python, NumPy, SQLite, locale, runner-file closure, or
    sandbox identity disagrees with that sealed descriptor.
  - [ ] Freeze the authoritative Linux OCI image and dependency artifacts by digest, then
    capture strict clean-host sandbox evidence under that exact identity.
- [ ] **Build one real offline pack.** Add a pack compiler and content-addressed source
  object store for the pinned Mathlib tree and declaration oracle, TheoremGraph inputs,
  sealed D1 annotations/community data, external normalized files, and curated Git inputs.
  Restricted or link-only raw objects remain local/non-exportable while their digests,
  acquisition receipts, normalization outputs, and policy still close the pack.
  Bind the verified `offline_pack_id` and real `source_set_root` into release attestations;
  retire the compatibility `legacy_declared_input_root` from authoritative releases.
  - [x] Make source-pack and semantic-diff integrity verification stream opaque files so
    large corpus objects do not require matching in-memory byte allocations.
- [ ] **Prove cross-object coherence.** Verify Mathlib archive ↔ declared commit,
  declaration oracle ↔ Mathlib revision, paired external pages/links ↔ one
  acquisition, TheoremGraph objects ↔ declared dataset revisions, and folded outputs ↔
  sealed proposal/source inputs.
- [ ] **Add the dual-build gate.** Build the same pack in two different absolute paths
  with randomized mtimes, isolated temp/cache roots, and adversarial `BRAIN_*` values, with
  network disabled at the runner/container boundary. Mount the verified pack as the only
  readable data input and use a separate writable output mount, then require byte-identical
  JSON/JSONL/static output plus equal base snapshot, projection, semantic, and release IDs.
- [ ] **Prove compatibility, not only repeatability.** Compare the clean-room result with
  the approved pre-refactor baseline and require zero graph/topology/content changes. Since
  replacing mtime/date pins intentionally changes provenance, require either a documented
  legacy-pin normalization comparison or an explicitly reviewed provenance-only migration
  report. Include fixtures for prior snippet-loss and fold/source-mismatch regressions.
- [ ] **Define a reproducibility attestation schema.** Record both build identities, pack
  and environment IDs, compared roots/digests, normalized provenance result, and pass/fail;
  the existing validation attestation is not a substitute.
- [ ] **Close provenance/license coverage.** Require every emitted provenance source to
  resolve to a sealed source manifest and policy entry. Resolve the current `tag-queue` and
  `wikilean` registry-name gaps and record explicit policy for nLab, OEIS, LMFDB, and each
  differently licensed TheoremGraph object before making this gate strict.

**Next P0-R implementation order:** (1) pin the complete Python/NumPy/SQLite/container
execution environment and exercise the narrowed sandbox on supported clean hosts; (2) compile
the first real full-corpus pack and prove cross-object/source-revision coherence; (3) add the
two-path randomized-mtime/adversarial-environment clean-room gate using the real OS boundary;
(4) run the approved-baseline semantic compatibility review and emit the separate two-build
reproducibility attestation. Network acquisition, live D1 snapshots, and proposal folding
remain outside the replay boundary throughout.

**Done when:** two clean-room full-corpus builds from one verified pack are identical;
touching files changes nothing; undeclared, missing-required, substituted, or silently
appearing optional inputs fail before reduction; graph/content parity plus the reviewed
provenance migration are proven; and the reproducibility attestation is stored with the
release.

#### P2A — shadow assertion kernel `[PARALLEL, SHADOW ONLY]`

- [ ] Freeze the v1 operation envelope and assertion state plus the minimal
  operation family: entity assertion, relationship assertion, assertion retraction, and
  exact-retraction restoration. Use an explicitly experimental fixture wrapper and fixture
  relationship-kind policy—not accepted `changeset/v1`—so source transitions, reducer
  schedules, semantic migrations, and authoritative kind policy can wait for P0-R without
  mutating a frozen schema. Do not conflate authored kinds with generated `depends`/bulk
  `links` edges.
- [ ] Implement `brain/tools/validate_authority.py` and
  `brain/tools/replay_authority.py` using the existing canonical JSON/hash primitives.
  The semantic root must bind inactive assertions and exact retraction history, not only
  the currently active graph.
- [ ] Add stable operation/assertion IDs, assertion revisions, predecessor/root checks,
  tombstone state transitions, derived conflict footprints, and deterministic full plus
  incremental replay.
- [ ] Shadow-import `brain/data/container_links.jsonl` and
  `brain/data/discovery_proposals.jsonl` first. Prove canonical source-contribution parity
  against those legacy rows; leave runtime inputs and `authority.through_changeset`
  unchanged. Full graph semantic parity waits for P2B's compatibility exporter and P0-R
  build context.
- [ ] Add adversarial fixtures for unknown versions/fields, duplicate or reused IDs,
  authored `cell:` endpoints, stale expected revisions, bad predecessor roots, invalid
  retract/restore chains, and equivalent independent assertions. Prove independence from
  physical file enumeration and JSON serialization order; permute only operation classes
  whose commutativity is explicitly registered and tested.

**Done when:** fixture full/incremental roots converge, the pilot legacy-vs-shadow semantic
contribution diff is empty, and no production bytes/routes change. Genesis acceptance and
cutover remain blocked on P0-R and the P1C rollback drill.

#### Later authority and overlay milestones

- [ ] **P2B — complete authority semantics (depends on P0-R):** freeze accepted
  `changeset/v1`, source-transition, reducer-schedule, semantic-migration, and authority
  policy contracts; add `attach_evidence`, curated attributes, proposal accept/reject,
  verification/veto, supersession with cycle prevention, and alias/merge/split identity
  operations. Test source-name-to-manifest state, prior-manifest continuity, missing
  objects/manifests, duplicate transitions, source-set-root convergence, migration fixtures,
  and the compatibility exporter that feeds the P0-R build context.
- [ ] **P2C — protected genesis:** inventory every legacy input family as assertion,
  decision/evidence, source transition, compatibility-only archive, or excluded with a
  rationale. Compile a candidate genesis outside accepted `changesets/`; preserve
  independent equivalent assertions and avoid original/verified/folded double counting.
  Put first-parent validation, exactly one changeset in
  authority-changing commits (otherwise an explicit no-transition declaration), protected
  append-only paths, and the duplicate-ID registry in read-only CI. Account for shallow
  GitHub checkouts. Implement the actual landing-head CAS in branch protection/merge queue
  or a separately reviewed authorized landing tool—never broaden validation-only
  `.github/workflows/ci.yml`. Review the candidate and full semantic parity, but do not land
  genesis yet.
- [ ] **P2D — authority cutover:** quiesce every named legacy curated writer, seal the
  inclusive watermark/digest, regenerate or verify the candidate against that exact
  watermark, then CAS-land genesis and enable authority validation. Make replay emit
  deterministic legacy compatibility exports for derived/folded inputs. Preserve original
  proposals, verified decisions, and other evidence under an explicit immutable archival
  disposition rather than regenerating them. Route
  `build_common.py`, `build_cells.py`, and `build_shards.py` only through replay state or
  those exports; order the nightly as replay → exports → release. Stop hardcoding
  `through_changeset: null`; verify the non-null chain/root before accepting a release, and
  retain old derived/folded files only as generated compatibility outputs.
- [ ] **P3A — append-only overlay foundation:** only after complete Phase 2 contracts, add
  a new ordered migration after `0012` (never edit applied migrations `0010`/`0011`) for
  operation, changeset, decision, rebase, promotion, and receipt tables. Deterministically
  backfill all `brain_nodes` plus live and deleted `brain_edges`, preserve legacy IDs as
  origin references and tombstones, do not collapse independently sourced equivalent
  assertions through `(src,dst,kind)` uniqueness, expose a compatibility current view, and
  require shadow read parity before writer cutover.
- [ ] **P3B — release-pinned atomic writes:** make node, edge, and bulk submissions one
  atomic changeset with base release, monotonic sequence, stable IDs, expected absence or
  revision checks, and idempotent retry receipts.
- [ ] **P3C — overlay generations and rebase:** implement deterministic classifications,
  immutable per-base materializations, atomic compatible release/generation selection, and
  retained rollback generations. Every overlay response exposes base release, generation,
  sequence, and effective graph token. Define the post-promotion hook that rebases remaining
  overlay operations against the new accepted base and prepares the next generation.
- [ ] **P3D — exactly-once promotion and recovery:** add fenced reservation/receipt,
  duplicate-promotion handling, crash-after-Git-before-receipt adoption, backup checksums,
  and measured restore/reconciliation RPO/RTO drills. Every successful or adopted promotion
  must run P3C's deterministic rebase, expose conflicts/orphans/quarantine, and atomically
  switch to the compatible new overlay generation.

PostgreSQL, R2, and graph-native serving remain measurement-triggered projections,
not current dependencies. Start PostgreSQL shadowing only for a demonstrated shared
history/concurrency need or after optimized representative queries miss 500 ms p95.
Start R2 shadowing when compressed releases exceed 500 MB, releases exceed 10,000
objects, deploys exceed five minutes p95, or Worker retention/rollback limits bind.

---

## P0 — Before public announcement  `[DEPLOYED 2026-06-10 — version 99a27390]`

Security + embarrassment fixes. Shipped to production via 4 parallel agents (disjoint
file ownership) + adversarial integration review + live smoke test.

- [x] **XSS-1 (launch blocker):** `status`/`provenance` now htmlEscape'd in wrap.ts
  attributes; `a.status` escaped in script.js. **DEPLOYED.**
- [x] **XSS-2 (launch blocker):** `a.mathlib_url` escapeHtml'd at the script.js href
  sink; `mathlibDocsUrl` now encodeURIComponent's module segments + decl fragment.
  **DEPLOYED.** (Smoke-tested: cross-origin POST→403, normal anchors intact.)
- [x] **Server-side validation** in POST /api/article/:slug: shared
  ANNOTATION_STATUSES enum (incl. `rejected` for future tombstones), per-field caps
  (MAX_FIELD_LEN=300 ids / MAX_TEXT_LEN=2000 free-text — raised from a too-tight 200
  that the review caught would 400 saves on 120 articles with long notes), payload
  caps (256 KB / 2000 → 413). Re-validated: 0 violations across all 1,369 files.
- [x] **Optimistic concurrency:** `window.__WL_VERSION__` injected; editor sends
  `base_version`; CAS-guarded UPDATE (`WHERE slug=? AND version=?`, 0-change→409);
  409 returns `{error:'stale', version, annotations}`; client reloads. Back-compat:
  absent base_version writes unconditionally. **DEPLOYED.**
- [x] **Role gate:** `requireRole` in auth.ts; revert gated to patroller/admin (403
  else); `role='blocked'`→getUser returns null (anonymous everywhere). Save stays
  open. **Jack seeded as admin (jack.mccarthy107@gmail.com).** DEPLOYED.
- [x] **app.onError** structured {event:'error'} logging + clean 500. DEPLOYED.
- [x] **Origin-header allowlist** on both write endpoints (vs request URL origin);
  `useSecureCookies:true` + `sameSite:'lax',secure:true` on better-auth. DEPLOYED.
- [x] **Editor: panel close** — × button + Escape-to-close + Cmd/Ctrl+Enter-save.
- [x] **Editor: save() spread fix** — `{...original, ...built}` preserves unknown
  fields (proof_note etc.). DEPLOYED.
- [x] **CC BY-SA attribution footer** on every article page (Wikipedia link + CC
  BY-SA 4.0 + annotations CC0). DEPLOYED + smoke-tested present.
- [x] **Data-collection notice** in editor panel footer + CONTRIBUTING.md ("Data &
  research notice") + token-donation policy ("Donating compute", marked planned).
- [x] **Mobile triage CSS** — bottom-sheet panel + wrapping bar @640px; overflow-x
  table wrappers in pages.ts. DEPLOYED.
- [x] **Cache prefix bump v6→v7** + asset `?v=` bumps (style/script v4, review v3,
  editor v5). DEPLOYED — evicts any XSS-poisoned cached pages.
- [x] **Deploy + verify live** (version 99a27390) + pre-deploy backup
  (backups/wikilean-20260610T070201Z.sql, 32 MB).

**P0 manual follow-ups:**
- [x] `CLOUDFLARE_API_TOKEN` repo secret added (account API token; needed **D1
  Edit**, not just Read — export creates a job via POST). Workflow verified
  end-to-end 2026-06-10: run 27260232307 green, 5 MB artifact (32 MB raw),
  nightly cron live at 08:27 UTC. Workflow made self-contained (no dependency on
  wiki/ in the checkout) and committed to main (c79296c).
- [x] **Backend committed to git (2026-06-10).** Jack's standing instruction: commit
  everything to Deicyde/WikiLean going forward — maximum version control. Excluded
  (gitignored): secrets (.dev.vars), node_modules/.venv/.wrangler/backups,
  re-fetchable site/cache/*.html + derived sections.json (351 MB + 35 MB),
  generated site/out (241 MB), generated seed/delta/refresh.sql, wiki/public build
  output. INCLUDED deliberately: site/cache/*.meta.json revid sidecars (5.4 MB —
  NOT reproducible; they pin which Wikipedia revision each article was annotated
  against). Secret scan run pre-commit: no values, only env-var name references.
- [ ] Minor: bump actions/checkout + setup-node for the Node 24 runner migration
  (GitHub deprecation notice; forced June 16, 2026 — low stakes, fold into next PR).

**P0 asset pipeline note:** canonical sources are `site/assets/{script.js,review.css,
style.css}` and `wiki/assets/editor.js`; `wiki/scripts/build-public.ts` copies them
into `wiki/public/assets/`. Brain assets and `brain.html` are the exception: they
must come from one explicit verified frozen release. Edit sources, freeze/verify,
then run build-public with that release; never edit `wiki/public/assets/` directly.

## P1 — Close the loop (the core re-architecture)

- [x] **One-time rescue pull** — DONE 2026-06-10. `wiki/scripts/pull-annotations.ts`
  (`npm run pull`); 709 rows pulled: 47 sidecars created, 8 real content updates
  (the 7 user-edited slugs' human edits rescued + Tangent_bundle stale-sidecar fix),
  manifest at site/annotations/.d1_pull_manifest.json. Human edits now in git.
- [x] **Stable annotation IDs** — DONE 2026-06-11, applied to production: 31,394 ids
  across 706 articles (CAS-guarded SQL, idempotent re-run verified, zero drift).
  Worker lazy-heal ADOPTS stored ids on sig-match (identity continuity), mints
  fresh only for new; malformed/duplicate → 400. Editor stamps on add. Runner
  echo-validates (unknown/missing id → inherit-by-sig else fresh).
- [x] **Worker API read/write path** — DONE 2026-06-10 (deployed 613da078). GET
  /api/article/:slug.json (public); bearer branch in getUser vs PIPELINE_TOKEN
  secret (backed by users row 'pipeline', role 'bot'; kill switch = delete row or
  role='blocked'); bot POSTs REQUIRE base_version (400), may carry revid (atomic
  re-pin in the same UPDATE) + meta (revisions.meta, 16KB cap); revisions
  kind='pipeline'/'edit'/'revert' + parent_id stamped. Token in wiki/.dev.vars.
- [x] **Server-side provenance stamping + human preservation** — DONE. Session
  saves: changed/new → forced 'human', unchanged keep stored provenance (anti-
  laundering both directions; judged with provenance stripped). Bot saves:
  provenance verbatim + findLostHuman 422 if any stored human annotation
  (incl. tombstones) is missing or altered (deep-equal, id-else-anchor-sig match).
  Anchor-only carve-out for update jobs still TODO when stage-1 re-anchoring lands.
- [x] **Tombstones** — DONE. Editor delete on persisted annotations → status
  'rejected' + provenance 'human' (spread-preserving); never-persisted still
  splice. Both engines skip rejected in lockstep (matched=true semantics — not
  anchor rot); excluded from badges + anonymous __WL_ANNOTATIONS__ (null
  placeholder keeps data-anno-indices aligned); editors still see/undo vetoes.
- [x] **moderation_state + GET /api/work** — DONE. Bot-only; modes review|wp-update;
  priority: flag_count DESC, wp_drifted DESC, human-edited-since-review,
  last_reviewed_at ASC NULLS FIRST; per-row reason string. Bot saves upsert
  last_reviewed_at/version + conditional wp_drifted reset.
- [x] **Unified runner `site/moderate.py`** — DONE (new|review|wp-update|all;
  --auth subscription|api-key via guarded key-pop; WIKILEAN_MATHLIB env; ID3 meta
  with ladder + id-discipline stats; 409/422/429 handling; D1-backed selection via
  /api/work). update_old_annotations.py removed (v1/v2→v3 migration complete);
  seed-delta/refresh retired to legacy. [GAP CLOSED — contract D-C1: bot-only
  `PUT /api/article/:slug` create endpoint shipped (index.ts, 201/409/400-reserved,
  full row init incl. counts + moderation_state + events); `new` mode creates via
  PUT (moderate.py build_create_body/put_article) — verified 23/25 created in the
  2026-06-18 run. This note previously claimed "no D1 create path — POST 404s";
  stale since D-C1 landed. Corrected 2026-08-04.]
- [x] **Wikipedia drift detection** — DONE (cron 17 6 * * * deployed; first tick
  pending). wiki/src/drift.ts: prop=info batches of 50, ≤8 batches/run with KV
  cursor (drift:cursor in RENDER_CACHE), full sweep every ~2 days at 709 articles.
  Drifted → latest_revid + moderation_state.wp_drifted=1; missing → state
  'deleted'; redirect → 'moved' (NB: redirects=0 param deliberately OMITTED —
  MediaWiki treats presence as true). Never bumps version. Staleness banner
  injected per-request when latest_revid > revid, with ?diff=cur&oldid= link.
- [x] **Stage-0 re-pin** — DONE (site/update_from_upstream.py; render.py gained
  target_revid + revid-keyed cache, legacy path byte-identical). FIRST PRODUCTION
  RUN 2026-06-11: 8/10 drifted articles re-pinned cleanly (incl. 102/102 anchors on
  Algebraic_K-theory); 2 held back with failing anchors recorded to
  .wp_update_report.jsonl. Stages 1-2 still gated on telemetry volume. Hazard on
  record: 'if'→'iff' edits keep high text similarity but invalidate formalization.
- **DRIFT REALITY CHECK (cron tick 1, 2026-06-11):** 145 of the first 400 articles
  (36%) had drifted from their pinned revisions. Upstream churn is much higher than
  assumed — wp-update is a first-class workload, not an edge case. Stage-0 clears
  ~80% of drift for zero tokens (first-run sample).
- [x] **Anchor-rot telemetry (log increment)** — DONE 2026-08-04 (fd64e7d9):
  cache-miss renders log {event:'render', slug, version, revid, matched, total}
  from the wrap engine's real results. REMAINING: articles.anchored_count column
  (needs a migration; write it only from live-pinned renders).
- [x] **Staleness banner** — shipped in Wave B (per-request injection, post-cache,
  ?diff=cur&oldid= link); this line had drifted from the Wave B log. Verified
  live 2026-08-04 (Banach_space shows the pinned-revision banner).
- [x] **Dynamic homepage/sitemap from D1** — DONE 2026-06-12 (Wave D). GET / and
  /sitemap.xml render from per-article count columns (KV-cached 5min/1h); static
  copies removed from build-public so the Worker routes aren't shadowed.
- [x] **Integration test harness** — DONE (Wave A, extended every wave since;
  111 tests incl. the full edit-safety cycle: seed → human save → bot echo →
  intact / bot drop → 422).
- [x] **WP_HTML TTL** (90d, Wave A) + delete-old-key on re-pin (Wave D).
- [x] **Token-budget memo** — refreshed 2026-08-04 with August run data (448b5e69,
  docs/token_budget.md). Original scope: tokens/article (from cache/.batch_run.log) × corpus ×
  cadence vs Max-plan limits. Gates the "AI-moderated" claim and sizes donations ask.
- [x] Fix serveArticle double-read race (Wave A).
- [x] Remove the GET-path revid write (Wave D; all 709 revids verified non-null).
- [x] discover_articles.py (Wave C) → feeds moderate.py new --from-file (Wave D).

## P2 — Experiment instrumentation + contribution UX

- [ ] **One revisions migration:** kind (edit|revert|seed|contribution|pipeline),
  meta TEXT (run_id, model, tokens, cost, mathlib_sha, auth_mode), parent_id, run_id.
  Backfill (comment LIKE 'revert to #%' → revert; user_id IS NULL → seed) BEFORE
  first bearer write.
- [x] **annotation_events table** — DONE 2026-06-12 (Wave D, migration 0005).
  Field-level diffs by annotation id on every write path; event types add|modify|
  delete|endorse|reject|revert_restore; actor session-vs-bearer. Endorse is now an
  explicit action (POST {action:'endorse', annotation_id, base_version}) since
  stampProvenance deliberately reverts bare provenance flips.
- [x] **Ladder stats** — DONE (Waves C + fix wave; moderation_flag dissent
  harvested per F14; flows into revisions.meta and the decisions sidecar).
- [x] **decisions.jsonl + pipeline_runs** — DONE 2026-06-12 (P2 wave): per-article
  decision lines (outcome taxonomy posted|noop|409-rebased|422|error|dry-run) in
  site/cache/.decisions.jsonl; pipeline_runs table (migration 0006) + POST /api/runs
  (idempotent); runner registers real runs, tolerates pre-deploy 404s.
  DEFERRED pieces: per-annotation confidence/considered fields + tool transcripts
  (need an Agent-2 output-schema change; see research-plan RQ6/RQ7).
- [x] **Anonymous flag pipeline** — DONE 2026-06-12 (Wave D). flags table by
  annotation_id; POST /api/flag/:slug (no auth, FLAG_LIMITER 5/min/IP, 5-open cap
  silent); ⚑ micro-form in the tooltip; /flags patrol queue with role-gated
  resolve; flag_count feeds /api/work priority. Verified live end-to-end.
  Turnstile remains the documented escalation if abuse appears.
- [x] **Patrol tooling** — DONE (diff pages Wave D; kind filter + patrolled_by/at
  + mark-patrolled with CAS, P2 wave migration 0006).
- [x] **Full Mathlib decl index** — DONE 2026-06-12: 411,273 decls from doc-gen4
  declaration-data.bmp, 849 recursive-prefix shards (<400KB each) in
  public/assets/decl-index/ (rebuild: npm run build:decl-index); editor
  autocomplete = curated boost tier + full-index shards + on-blur existence tick
  (never blocks saves). Server-side oracle consumption deferred to the
  contribution gauntlet.
- [x] **/stats + research export + research plan** — DONE 2026-06-12: /stats
  (public, RQ-labeled live counts, 300s KV cache); GET /api/research/export.jsonl
  (bot/admin, streamed, pseudonymized — sha256(user_id+salt), no PII) + nightly
  artifact riding backup-d1.yml (PIPELINE_TOKEN repo secret set);
  docs/research-plan.md (RQ1-RQ8 with exists-today status per question).
  NOTE: annotation_events is legitimately ZERO so far (re-pins echo verbatim,
  no-op reviews skip events) — first substantive edit starts the dataset.
- [x] Editor save UX — DONE 2026-08-04 (fd64e7d9): kind/match_kind selects
  (unknown stored values preserved), comment cleared only after 2xx, panel title
  by label/quote, alt-click opens Mathlib docs, orphaned-anchor Re-anchor flow
  (__WL_MATCHED__ distinguishes true rot from overlap-suppressed highlights —
  the latter get truthful copy and NO Re-anchor). REMAINING: in-place body swap
  (deliberate refactor with initAnnotations(), NOT a line item).
- [ ] **Propose-then-approve: AI may propose updates to human annotations** —
  designed in [propose-then-approve.md](propose-then-approve.md), awaiting Jack's
  UX pick (§5) before build. Foundation already exists (dormant
  `moderation_state.proposal` column + F14 `moderation_flag` harvest + `endorse`
  template); the agent never mutates a human annotation — it proposes, Jack
  one-click approves. `findLostHuman` 422 stays the floor. Jack's directive:
  "human-curated does NOT mean it shouldn't be updated by reviewers."
- [x] Trust signals — DONE 2026-08-04 (fd64e7d9): "N/M human-reviewed" badge
  (tombstones excluded both sides), keyboard-accessible legend popover
  (viewport-clamped), "Least recently reviewed" strip on / (moderation_state
  NULLS FIRST, parked states excluded).
- [ ] Privacy: stop storing session ip_address if better-auth allows; IRB exemption
  filed before any paper.

## P3 — Deferred, with written triggers

| Item | Trigger |
|---|---|
| jobs/contributions/api_tokens queue + validation gauntlet + trust ladder (design is written — see audit; atomic claim via single UPDATE…RETURNING) | First real compute donor asks |
| GitHub Actions donation fork template; Batches-API path for tool-free Agent 1 (50% cost) | First donor on the Actions path |
| Schema v4 `formalizations[]` + libraries registry (write docs/schema_v4.md anytime) | CSLib covers a typical undergrad algorithms course; bundle migration with ID backfill if still pending |
| Mass-revert: offline admin script over revision snapshots (NOT a deployed endpoint; D1 Time Travel is whole-DB and not a substitute) | First vandalism spree |
| Re-anchoring stages 1-2 (fuzzy + AI semantic) | Anchor-rot telemetry shows stage-0 clears <90% |
| WikiProject CS corpus ingestion, Agent 2 multi-checkout, library picker UI | With schema v4 trigger |
| physlib evaluation (its "informal definition" stubs must NOT count as formalized — needs a distinct match_kind) | When physlib registration is proposed |
| **/review posting via least-privilege GitHub App — CODE SHIPPED 2026-06-20 (review.ts: no-scope authorize w/ Iv-prefix sniff for zero-downtime, refresh-token-aware reviewToken, installUrl on 403).** Replaces the `public_repo` OAuth app (write to ALL the reviewer's public repos) with a GitHub App scoped to **Pull requests: write**; **user-to-server** tokens still post *as the reviewer* (preserving settle.py's maintainer-by-author gate). REMAINING (Jack): register the GitHub App (perms Pull requests + Issues: R&W; callback /review/auth/callback), set REVIEW_GITHUB_CLIENT_ID/SECRET to it + REVIEW_GITHUB_APP_SLUG. Then an **org owner must INSTALL** it on leanprover-community to actually post there — GitHub Apps do NOT bypass org approval (researched: user-to-server write needs the app installed). **DONE 2026-06-20 + cutover live (client_id Iv23…, no scope).** **ALSO shipped option A (REVIEW_POSTING_PAT):** a classic `public_repo` PAT used to post ONLY for its owner (the submitter must be connected AS the PAT account — public-endpoint-safe), so Jack can post his own reviews to mathlib **today, no install** (classic PATs are exempt from OAuth-App restrictions). The App stays the least-privilege *multi-reviewer* end-state; Copy review is the always-works fallback. | App install on lean-community (org owner) only when reviewers beyond Jack want one-click posting |

## Standing risks & invariants (check before touching these areas)

- Any change to wrap output bytes requires a render-cache prefix bump (render:vN).
- Any new D1 write path outside the Worker must bump `version` or readers see stale
  pages for up to 30 days.
- editor.js / review.css / script.js changes require `?v=` bumps (pages.ts and
  engine/page.ts).
- Never re-seed D1 from disk; transform D1 blobs in place (human edits live only in D1
  until the rescue pull, and are canonical in D1 always).
- revisions.user_id NULL = system convention is load-bearing until the kind/meta
  migration lands; retire NULL-keyed logic and backfill in the same change.
- The ~9 pending D1 schema changes go through ordered wrangler d1 migrations.
- Sequencing hazard: do NOT remove seed-refresh's user-edit skip or run --moderate
  against user-touched slugs before the D1 read path is live and verified.
- EDIT_LIMITER is per-isolate (advisory, not global); don't treat it as a hard cap.
- Wikipedia page moves/deletions: drift cron must handle redirect/missing or the
  update loop wedges on first contact.

## Review + eval infrastructure (added overnight 2026-06-12)

- **Four-agent review pass** (moderation workflow + UI, both adversarial): 16
  workflow findings (4 HIGH: wrong-revision reviews F1, flagged-queue livelock F2,
  drift-tier token misdirection F3, moved/deleted unconsumed F4) + 12 UI findings
  (4 pre-announcement: .html flag 404s, unreachable tombstone recovery,
  guaranteed-fail revert buttons, keyboard-inaccessible tooltips) + 3 test-agent
  findings (anchorSig anchors[] blind spot, no-op save churn, bot /flags access).
  All triaged into one fix wave. The "verified-solid" lists from both reviews are
  in the agent reports (session transcripts) — the 422 human-preservation core
  held under every constructed interleaving.
- **Test suites**: 334 Worker tests (authz matrix 13 endpoints × 8 actors;
  /api/work ladder; annotation_events integrity incl. zero-events-on-failure;
  boundary sweeps) + 45 Python tests + cross-language parity harness
  (wiki/test/fixtures/parity.json, 74 cases consumed by BOTH vitest and Python —
  pins the three sig/match/equality implementations against drift; 6 genuine
  divergences found and pinned, 1 crash-grade fixed in the fix wave).
- **Moderation evals**: site/eval_moderation.py --offline — 6 planted-defect
  scenarios (drop-human, tombstone-resurrect, id-rename, provenance-downgrade,
  create-launder, coverage-extend) through the REAL deterministic pipeline,
  scorecard + non-zero exit as a CI gate. --live mode exists but is token-gated;
  never run it in CI. Routine commands:
  `python3 site/test_moderate.py && python3 site/test_parity.py &&
  python3 site/eval_moderation.py --offline` and `cd wiki && npm test`.

## Status log

- 2026-08-06 — **Decl-existence sweep applied to production; first proposals
  filed.** Three-agent adjudication of all 337 missing decls (321 renames — 261
  verified / 43 high / 17 judgment — 8 clear_decl, 8 leave, 11 proof_wanted
  overclaims; 12 wrong sweep suggestions corrected). apply_sweep_verdicts.py
  (dry-run default, echo-verbatim bot path, live precondition re-checks):
  dry-run revealed 358/400 targets ALREADY fixed in D1 by nightly reviews +
  the July rename cleanup (disk mirrors lagged); applied the real gap — 42
  auto edits across 35 articles + 14 proposals (11 overclaims → partial, 3
  human-owned fixes incl. Picard–Lindelöf) — converged to 0, zero 422s.
  Corpus miss rate 2.96% → 0.48% (440 → 73 citations; residue = judgment-tier
  + overnight pipeline drift). PROPOSE-THEN-APPROVE IS NOW EXERCISED
  END-TO-END: /stats shows 15 pending / 0 decided — Jack's triage at
  /proposals is the next human touch. Mirrors re-pulled from D1.

- 2026-08-04 (evening) — **Frontier/halo reconciled + tombstone layer destroyed
  (821edf18, DEPLOYED cf39e3d5, live-verified).** Jack's directives: (1) hop-count
  shells hid bond volume — replaced by the formal-proximity contract (SCHEMA
  "Formal proximity": direct raw synapse weight into decl cells + bottleneck-
  capped bridge/4; midrank-percentile radius; six parallel prox arrays; per-
  library client re-scoring with exact-float parity, proven over six 2-library
  subsets). One frontier view, territory sectors, no toggle, no "jumps" copy;
  old-vs-new: a 648-bond cell and a 1-bond cell both sat in "shell 1", now
  separate 986.75 vs 1.0. Mobile stage-SVG collapse (≤900px painted 150px)
  fixed in the same pass. (2) Tombstones deleted outright: /map /graph /atlas
  /article-graph 301s, graph_data/atlas_data//api/atlas 410s, GET
  /api/brain/node, MCP brain_node alias — RESERVED only squats the names;
  clean 404s pinned by wiki/test/retired-routes.test.ts. Suites: frontier
  54/54, shards 56/56, acceptance 21/21, wiki 34 files / 706.

- 2026-08-04 — **Full-day autonomous sweep (14-task plan).** (1) Frontier/halo
  tranche verified + committed; worktrees/branches pruned; docs de-drifted.
  (2) Worker batch 1 DEPLOYED + verified live: unified nav across all shells,
  header article search (combobox a11y, prefix-ranked, left-anchored dropdown),
  dynamic /about from D1, [edit]-link hiding incl. the .mw-editsection-like
  excerpt variant; /api/articles index + nosniff; RESERVED swept
  (atlas_data.json, brain.html, concepts.html); ELOOP-causing brain/brain
  symlink removed + build-public made symlink-proof. (3) Pipeline reliability:
  ABORT_AFTER now bounds a bad night; reset-aware Max retries; create-path
  verified. (4) Data hygiene: count backfill, untagged articles, concept graph
  rebuilt against the annotation layer. (5) Brain: root panel follows zoom-out,
  halo boot guard + legible hint legend (deployed with batch 1); v2 per-node
  asset layer RETIRED (2b808365 — strict atom oracle, /api/brain/node 410,
  cell:-fixpoint endpoint normalization pinned by test, allow-list deploy
  450→94MB). (6) Trust signals + editor save-UX + anchor-rot telemetry
  (fd64e7d9, adversarially reviewed; render:v16, page:home:v10,
  editor.js?v=16). Suites at end of day: tsc clean, 33 files / 703 Worker
  tests, parity 7/7, moderate 106/106, cell shards 56/56, acceptance 21/21.
  **DEPLOY PENDING for 2b808365 + fd64e7d9** (permission classifier blocked
  `npm run deploy`; live /api/brain/node still 200 until it ships).

- 2026-08-02/03 — **Mathlib halo view + discoverability.** brain: halo view renders the frontier cells in distance shells around the formal core, niche topics outward (03d46a7b); made discoverable via a frontier|halo toggle at the root + a rootsPanel link (52742028).

- 2026-08-02/03 — **Bridge report v3 complete.** bench: v3 paper restructure + supplement split per external review 2 (eda5d5a0) → adversarial-gate fixes, all 34 findings applied + held-out oracle validation 90% (84b097a3) → final production pass: prose gate, anchor notes, repaired-grid figures, typeset edition (215a059b). Paper + supplement complete in docs/research/ (BRIDGE-REPORT.md, BRIDGE-SUPPLEMENT.md + bridge-report/ report.pdf/supplement.pdf); harness in bench/.

- 2026-08-01 — **Frontier data layer (B1).** brain/build_frontier.py partitions the 1,612 homeless (decl-less) cells into 46 deterministic `frontier:<Area>` territories (synapse vote ×3 depends/invocation → MSC xref → one relates hop → Unsorted 309 = 19.2%); build_cell_shards emits them as parentless supercells.json rows so the bubbles view's grey "no formal home" blob drains to the 5 genuinely unplaceable cells; contract doc in brain/SCHEMA.md "Frontier layer", acceptance F1–F8 (brain/test_frontier.py) nightly-wired; UI branches for `frontier:` ids (panel/status/zoomOut) are the follow-up tranche.

- 2026-08-01 — **TauCeti wired into the fabric.** Deterministic decl→decl `invocation` edges (FQ-name-in-statement scan, suffix-proof, oracle-restricted: TauCeti 6,174 + FC 2,853, all surfacing as synapses — never merges) + a generic frontier-repo agent-join channel (fold_proposals `repo_link` action per frontier_sources key → catalog/data/`<key>`_links.jsonl, mentions-ONLY by moderation contract; sync_agents `linker` role + repo_link skeptic, nightly-wired via WIKILEAN_BRAIN_REPO_MODULES). First bounded TauCeti linker pass in flight; its folded joins land as `mentions` synapses once the skeptic + fold complete.

- 2026-07-25 — **Generic Lean-repo frontier ingester live** (brain/ingest/lean_repo.py + build_common's parameterized `_frontier_repo_layer`; FC refactored onto it byte-identically): TauCeti minted as frontier client #2 (7,858 decl:TauCeti:* nodes @ b67da432056b), user-registered repos wired end-to-end (weekly /api/repos/enabled sync → per-repo harvest, caps 50 repos / 20k decls, provenance `user_lean_repos`); acceptance P12–P14 guard the registry entries + count conservation.

- 2026-06-16 — **Search-verified moderation + nightly automation live.** Reviewer
  search skills (.claude/skills/{mathlib,wikidata,wikipedia}-search) built and
  wired into Agent 2 as SDK custom tools (site/search_tools.py) — quality read of
  a real batch confirmed they FIX stale/hallucinated Mathlib decls
  (Basis.ofVectorSpace→Module.Basis.ofVectorSpace etc.), not churn. Durability:
  checkpoint-and-retry-POST (moderate.py flush) + run-level revert endpoint
  (deployed). NIGHTLY launchd schedule live (site/ops/, 03:00 local, flush→
  wp-update→review, token-capped) — needs Full Disk Access on /bin/bash (Desktop
  TCC); verified end-to-end (Max auth works under detached launchd). Security:
  wiki login narrowed to identity-only — public_repo dropped (it was leaking
  repo-write to every editor via the shared review-tool OAuth; that tool now
  needs its own GitHub OAuth app).

- 2026-06-12 (overnight autonomous run) — **UI redesign + review pass + eval
  infrastructure + fix wave + P2 completion, all deployed.** (1) Warm
  academic-minimalist redesign across homepage/shells/article chrome. (2)
  Four-agent adversarial review: 31 findings; the fix wave closed all of them —
  headline: F1 reviews-against-wrong-revision (verified fixed in production:
  suffixed pinned-revid cache), F2 flagged-queue livelock (verified released
  live), F3 stage-0 wired into the runner (12 more drifted articles re-pinned,
  zero tokens), F4 moved/deleted parked out of selection, revert CAS, no-op
  save short-circuit. (3) Tests 115 → 436 Worker + 76 Python + 82-case parity
  harness + 7-scenario eval gate. (4) P2 complete: /stats, pseudonymized
  research export (nightly artifact), pipeline_runs + decisions.jsonl,
  411k-decl Mathlib index with editor autocomplete + existence tick, patrol
  kind-filter + mark-patrolled. Migration 0006 applied; deployed 4b70fb99.
  Remaining in P2: editor save-UX niceties (selects, in-place body swap),
  trust badges, ip_address storage decision. P3 items unchanged (triggers).

- 2026-06-10 — Roadmap created from architecture audit. P0 started.
- 2026-06-10 — **P0 deployed** (Worker version 99a27390). 4 parallel coding agents on
  disjoint files (backend/auth, render/XSS, editor/frontend, ops/docs) → adversarial
  integration review (found + fixed the MAX_FIELD_LEN=200 blocker) → deploy → live
  smoke test (XSS sinks escaped, 403 cross-origin, CC BY-SA footer, 401 anon). Jack
  promoted to admin. Pre-deploy D1 backup taken.
- 2026-06-10 — **P1 Waves A+B shipped.** Wave A: rescue pull (7 articles' human
  edits now on disk + git), 18-test integration harness, migration 0004 applied to
  prod (949 seed/15 edit backfill verified), WP_HTML TTL + double-read fix, budget
  memo ($1.34/article; quarterly review of 709 solo-feasible). Wave B (deployed
  613da078, 64/64 tests): bearer pipeline path + provenance stamping +
  human-preservation 422 + /api/work + tombstones + drift cron + staleness banner.
  Pipeline user seeded; PIPELINE_TOKEN secret set (value in wiki/.dev.vars).
  Smoke-tested live: :slug.json shape, /api/work 403/jobs, bot-save 400 contract.
  Note: numeric-slug articles (0, 1, 100…) checked — genuine number articles, not
  junk. Next: Wave C (ID backfill, moderate.py, wp-update stage-0, discovery).
- 2026-06-11 — **P1 Wave C shipped — THE LOOP IS CLOSED.** Stable IDs applied to
  production (31,394 annotations, 706 articles, idempotent-verified); Worker
  lazy-heal deployed (d547d917, 74/74 tests); moderate.py runner live (dry-run
  verified: ids_echoed 35/35, fresh 0); drift cron tick 1 found 145/400 drifted
  (36% — far above assumptions); FIRST PRODUCTION STAGE-0 RUN re-pinned 8/10
  drifted articles for zero AI tokens, 2 recorded for stage-1. First real AI
  review pass (2 articles) launched. Remaining P1: article-create endpoint for
  `new` mode (POST 404s on unknown slugs — C2 friction), dynamic homepage/sitemap,
  GET-path revid write removal, WP_HTML delete-on-re-pin. Then P2 instrumentation.
- 2026-06-11 — **FIRST REAL AI REVIEW RUN VERIFIED (run 9abcf468).** 2 articles
  (the queue correctly surfaced stage-0's two needs-work articles first), 0 errors,
  ~$2.96 equiv / 707s. D1 round-trip confirmed: revisions kind='pipeline' with
  parseable comments + full meta (ladder, tokens, id discipline — Addition echoed
  69/69 ids through both agent passes; article '0' got 4 fresh ids for coverage
  extensions, 35→39 annotations, anchors now 39/39); moderation_state stamped so
  both sort to the back of the review queue. The three-script lifecycle from the
  project goal is now: generate=moderate.py new (needs create endpoint), review=
  WORKING, update=WORKING (stage-0).
- 2026-06-10 — **VERSION-CONTROL RISK SURFACED:** `wiki/`, `site/`, `docs/`,
  `CONTRIBUTING.md` are untracked in git — the whole live backend has never been
  committed. P0 (and everything before it) exists only in the working tree + the live
  Worker + D1 backups. Recommend an initial commit of the backend on the `p0-hardening`
  branch before P1. (Not done autonomously — it's a large one-time decision for Jack:
  what to track, `.gitignore` for `.dev.vars`/`.wrangler/`/`backups/`, etc.)
  [RESOLVED 2026-06-10 later that day — backend committed (bdea253f); see P0
  "Backend committed to git".]
