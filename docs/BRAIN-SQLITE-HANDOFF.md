# Brain SQLite operational handoff — 2026-09-05

This is a branch-state and operator handoff, not the project plan. The canonical plan and
completion criteria remain in [`ROADMAP.md`](ROADMAP.md), especially P0-R and P1A–P1C.
Architecture details live in [`BRAIN-ARCHITECTURE.plan.md`](BRAIN-ARCHITECTURE.plan.md), and
the executable authority contracts are summarized in
[`../brain/authority/README.md`](../brain/authority/README.md).

## Laptop-transfer checkpoint

Resume from branch `codex/brain-architecture-phase1` and pull `origin`. The latest
implementation checkpoint is `03129828 Preserve D1 evidence freshness`, after
`34fd6ee3 Update Brain migration handoff`, `39632d33 Preserve Wikidata evidence freshness`,
`4cd402c1 Seal Wikidata proposal evidence`, `85d30e54 Pin Brain replay pack identity`, and
`bc158cfd Integrate v3 Brain replay pipeline`. This handoff and roadmap update is committed
immediately after that implementation history.

```bash
git fetch origin
git switch --track origin/codex/brain-architecture-phase1
git status --short --branch
```

The worktree should be clean before continuing.

Immediate continuation order:

1. Configure a D1 Read-scoped Cloudflare token locally (never paste or commit it), run
   `brain/acquire-d1-snapshot.sh`, review the private bundle, and bind it into a v3 plan.
2. Restore a read-only Mathlib source checkout and author the complete reviewed v3 source
   plan, including the pinned Hugging Face inputs and license decisions.
3. Run the bounded v3 preflight and compile the first real pack on a volume with adequate
   headroom, then perform the two-path clean-room replay and semantic comparison.
4. Replace the still-separate legacy Wikidata universe/edge/description reads with one shared
   sealed observation generation, then bind it and the completed proposal-entity bundle into
   the reviewed v3 current-corpus source plan. Do not describe independent WDQS/Action API
   requests as a transactional upstream snapshot.

No Worker deployment, production promotion, D1 write, operator Wikidata bundle acquisition,
or tracked-data regeneration was performed in this stabilization session. Read-only API
compatibility probes published no artifact and changed no production state.

## Bottom line

The SQLite architecture and its fixture-scale sealed replay path are substantially
implemented. This branch does **not** yet establish an authoritative full-corpus build:

- no reviewed current-corpus source plan exists;
- no real `offline-pack/v3` has been compiled;
- no full-corpus clean-room replay, dual-build equality result, or reproducibility
  attestation exists;
- no verified v3 pack identity has been bound into an authoritative release; and
- nothing in this work authorizes a Worker deployment or production promotion.

Production remains on the compatibility authority path. P1 activation remains a separate,
Jack-approved operation after review and merge; do not run `npm run deploy` or the release
promoter as part of this handoff.

## What is implemented here

Branch: `codex/brain-architecture-phase1`. At this handoff it contains:

- the indexed SQLite projection with deterministic JSONL parity, stable logical/raw
  identities, atomic publication, cells, synapses, planner statistics, and bounded probes;
- immutable Brain release assembly and streamed closure verification;
- exact-release promotion, durable deployment-journal, public-baseline, and activation-
  evidence tooling, all production-inactive;
- versioned source-manifest and offline-pack contracts through v3, plus reducer-inventory,
  build-context, execution-environment, and build-attestation contracts;
- standalone, self-identifying acquisition-receipt and normalization-lineage contracts
  with fail-closed request accounting, query-free evidence URIs, hashed request parameters,
  exact origins, and audit-only timestamps;
- copy-only sealed workspace preparation and a seven-stage, network-denied replay runner;
- all seven reducers routed through explicit context bindings rather than ambient paths,
  mtimes, clocks, or `BRAIN_*` identity inputs; and
- bounded-memory verification for large opaque source/release objects.

The latest sealed-pack milestone adds:

- stricter live environment probing for Python, NumPy's installed closure, SQLite, locale
  and hash settings, runner-file closure, development-host identity, and sandbox policy;
- canonical offline-pack source-plan schemas and a deterministic, network-free compiler
  that preserves v1-plan→v2-pack identity and emits v3 packs only from validated v3 plans;
- a bounded source-plan preflight that hashes and validates small control/evidence documents,
  uses Git/lstat sizes for corpus payloads, and reports separate `compile_ready`,
  `source_authority_ready`, and `source_publishable` states; and
- source-selector and test-gate updates for the current reducer inventory.

The report is explicitly `source-plan-only` with `runtime_environment_checked: false`; its
readiness fields are not runtime, replay, release, or deployment claims. The preflight CLI
returns `0` only when `source_publishable` is true, `2` for a valid but non-publishable source
plan (including warning-class authority/publication concerns), and `1` for structural or
argument errors. All report/error output is canonical JSON. Receipt-role detection remains
presence-only in v2. The explicit v3 source-manifest, source-plan, and offline-pack contracts
now validate and seal receipt, lineage, and request-preimage bytes with clock-free logical
identities. The compiler preserves exact v1-plan→v2-pack compatibility while emitting
evidence-closed v3 packs from v3 plans; preflight validates and accounts for evidence, and
preparation/replay accept only fully verified v2 or v3 packs.

The subsequent acquisition-integrity milestone adds:

- a shared `external-pair/v1` semantic identity and strict reader validation for newly
  emitted external page/link artifacts;
- per-database writer locking, canonical serialization, a durable transaction journal,
  hard-linked prior-generation recovery, and explicit zero-link partners, with coverage for
  caught failures, first-publication interruption, concurrent writers, and real `SIGKILL`;
- validation in the graph build, proposal fold, agent candidate generator, and acceptance
  gate, while explicit replay bindings reject orphan or unregistered source objects; and
- a sealed Wikidata `wbgetentities` acquisition command for proposal QIDs, with an isolated
  CPython 3.12 launcher, exact curl/request policy, canonical request preimages, complete
  response/bisection transcript, redirect and missing-identity validation, clock-free
  normalized bytes, receipt/lineage evidence, and atomic no-replace publication into a
  private content-addressed store. Receipt and lineage logical IDs exclude audit clocks,
  while the bundle directory ID hashes the exact canonical audit-bearing evidence bytes, so
  a fresh observation of unchanged upstream data publishes as a distinct immutable evidence
  generation;
- an independently implemented verifier that closes paths and members, replays request order
  and bisection, recomputes normalized entities, and checks toolchain, receipt, lineage, and
  requested-QID-set identity; and
- an offline-only proposal fold: it emits the canonical plan in planning mode, performs no
  network fallback, requires an explicit absolute verified bundle for a non-empty plan, and
  preserves every fold output byte on a plan/bundle mismatch or verification failure.

Brain nightly now makes that boundary a hard gate. It creates a private per-run directory,
validates the canonical plan, skips acquisition when the plan is empty, otherwise acquires
into `catalog/.cache/wikidata/entity-bundles`, validates that stdout names exactly one
content-addressed child of that store, and passes that explicit bundle to the fold. Any plan,
acquisition, path, verification, or fold failure aborts build and release. The Brain lock is
never stolen based on age; after confirming no owning process exists, an operator must remove
the stale lock manually. Cleanup removes only the known plan/stdout files and leaves an
unexpected nonempty run directory for inspection.

This completes acquisition separation for proposal folding, not for the whole Wikidata
surface. Current legacy external pairs remain readable; the universe, relation-edge, and
description jobs still perform separate live reads without a shared sealed generation; and
the checked-in legacy external JSONL bytes still contain observation/run metadata even though
new writer output does not. The proposal bundle receipt/lineage is also not yet bound into the
reviewed v3 current-corpus source plan, so this milestone is not an authority or production-
release claim.

The shared external-pair writer now emits clock-free bytes for identical normalized rows,
requires a nonempty source pin, and rejects run/cache telemetry and unknown metadata rather
than letting it churn source-manifest and pack identities. DLMF, EoM, Kerodon, and OEIS keep
their run counters in operator logs only; ProofWiki uses the exact dump SHA-256 and refuses to
publish if those bytes change during normalization. Legacy unsealed readers remain compatible.
This is normalized-output cleanup, not acquisition evidence: the removed telemetry still
needs durable receipt/audit representation when the shared sealed generation is implemented.

The latest Wikidata safety milestone makes the universe, relation-edge, and description
harvesters all-or-nothing and atomic. Typed response validation, deterministic
canonicalization, redirect handling, prior-generation/input-relative volume floors, and
label/slug/description coverage guards prevent successful-but-collapsed responses from
replacing known-good artifacts. Description output no longer merges a prior cache or embeds
run timestamps. The focused hermetic suite passes 27 tests. These remain separate live API
reads, not a shared upstream snapshot or sealed receipt/lineage generation; concurrent
writers are not yet serialized.

The immutable Hugging Face acquisition milestone additionally adds:

- reviewed full-commit, byte-count, and SHA-256 pins for all six files across
  `uw-math-ai/math-graph`, `uw-math-ai/theorem-matching`, and
  `MathNetwork/MathlibGraph`;
- exact-revision-only downloads, safe adoption of matching legacy caches, curl-config
  isolation, complete-set staging, separate writer/publication locks, durable rollback,
  and real `SIGKILL` plus concurrent-reader/writer coverage;
- full-read verification in every current Python consumer and the TypeScript premise-index
  builder, without adding acquisition policy to the sealed v2 reducer closure; and
- deterministic hierarchy/theoremgraph-link lineage plus immutable premise-index API pins,
  with backward compatibility for the deployed legacy mtime-shaped manifest.

These pins do not by themselves make v2 authority-ready. The v3 contracts now express and
verify acquisition receipts, lineage, and request preimages, and the compiler/preflight/
preparation/replay path accepts fully verified v3 packs. Current inputs still need real,
reviewed evidence and one authoritative current-corpus v3 plan.

The sealed D1 acquisition foundation additionally exists, but has not completed a production
capture in this branch. Run it only through `brain/acquire-d1-snapshot.sh`, which selects
CPython 3.12 and starts it with `-I -S`. One checked-in read-only statement covers articles,
every community edge (including tombstones), and community nodes. The producer checks row
counts and exact migrated column inventories; binds the production account/database UUID,
pinned Wrangler package, digest-bound Node 22 executable, exact Python executable/version,
and the local transitive Python dependency closure; then atomically publishes a private
content-addressed bundle containing
clock-free normalized objects and validated receipt/lineage evidence. Logical receipt and
lineage IDs exclude audit clocks, while the bundle directory binds the exact canonical
audit-bearing evidence bytes. Reacquiring unchanged rows therefore publishes a distinct
immutable observation with the new acquisition time instead of silently reusing stale
evidence. This incompatible publication identity is explicitly versioned as D1 acquisition
bundle v2; no production v1 bundle was captured. No source-plan or release authority claim
follows from the acquisition tool alone.

Community graduation and the annotation mirror are now wired to that boundary through one
shared independent bundle verifier. `harvest_community_edges.py` accepts only an explicit
sealed bundle and pins output provenance to the normalization-lineage identity. `npm run
pull -- --snapshot-bundle /absolute/path/to/<bundle-id>` builds the complete next annotation
cache off to the side, preserves exact JSON numbers, atomically exchanges generations, and
quarantines disk-only sidecars outside active selectors. Neither consumer can acquire live
D1 data or use a fixture bypass. Source-plan v3 authority is still not wired to the bundle,
and no production bundle has been captured.

Nightly operations are portable across checkouts. `site/ops/nightly-launchd.py` validates a
sparse launchd-like environment, seals the exact checked Python and Mathlib paths into
generated plists, and installs files without loading jobs. Community graduation is off by
default and requires an absolute reviewed bundle path; the moderation job never acquires D1
state implicitly.

For a manual proposal-fold acquisition, use one private run directory and keep planning,
acquisition, and folding as three explicit steps. Do not invoke the acquirer for an empty
`qids` array:

```bash
mkdir -m 0700 /absolute/private/wikidata-run
python3 brain/fold_proposals.py \
  --write-wikidata-request-plan /absolute/private/wikidata-run/request-plan.json
# If the plan has qids:[], run `python3 brain/fold_proposals.py` and stop here.
BUNDLE="$(brain/acquire-wikidata-entities.sh \
  /absolute/private/wikidata-run/request-plan.json \
  --store /absolute/private/wikidata-entity-store)"
python3 brain/fold_proposals.py \
  --wikidata-entity-bundle "$BUNDLE"
```

The launcher deliberately strips proxy variables and forces `NO_PROXY=*`; the operator host
therefore needs direct HTTPS egress to `www.wikidata.org`. Proxy-only environments fail
closed. Each response is capped at 64 MiB, but the current producer and verifier materialize
the aggregate transcript/bundle in memory. Keep plans operationally bounded and implement
streaming before materially scaling toward the 50,000-QID schema ceiling.

Frontier replay no longer accepts `manage/data/halo.json` as authority input.
`mean_stateability` is deterministically re-derived from the exact bound cells and synapses
with the historical ring-1 neighbor-fraction semantics. On the current corpus, all 47 area
IDs, membership, proximity, suitability, ordering, and top-cell choices are unchanged; 22
stateability summaries, the stateability input-count metadata field, and the reducer
inventory identity changed. The operational halo report remains available for management
worklists only.

## Verification state and required final commands

Focused results recorded through 2026-09-05:

- offline-pack compiler: 26 tests passed;
- source-plan preflight: 21 tests passed;
- authority contracts: 74 tests passed, including the v3 evidence and parent-DAG closure;
- sealed Wikidata entity acquisition and independent verification: 23 tests passed;
- offline-only proposal folding: 9 tests passed;
- external pair publication/reader fixtures: all checks passed;
- execution environment: 20 tests passed;
- replay executor: 36 tests passed;
- replay preparation: 29 tests passed;
- base-graph context: 11 tests passed after the annotation-selector fix; and
- replay sandbox: one expected local skip because strict clean-host evidence was not
  requested/available.

The latest continuation additionally passed 12 sealed-harvester/shared-verifier tests,
17 D1-acquisition tests, 19 annotation-mirror tests, 10 top-level-shard tests, 15
portable-launcher tests, and 18 Brain-nightly shell tests. The complete D1 acquisition and
consumer slice and the Wikidata proposal-acquisition slice received independent clean
P0/P1 audits after their identified blockers were fixed.

The remaining D1-consumer audit notes are P2: a hard kill after the atomic exchange can
leave a complete old-cache sibling for an operator to identify and remove; acquisition and
mirroring still buffer/clone the current corpus; bootstrap executable discovery trusts the
operator `PATH` before recording exact digests; and long-lived historical bundles would
need an explicit versioned verifier profile rather than weakening the current v2 policy.

Final branch-wide verification on the evidence-generation implementation passed the complete
40-command Python gate and the Worker gate (37 files / 845 tests). The Python result includes
the one expected local replay-sandbox skip; every required offline scenario ran. After the
explicit D1 bundle-v2 version bump, the current focused results are 17 D1 acquisition,
12 shared-verifier/harvester, and 19 annotation-mirror tests. The Wikidata-focused results
remain 23 acquisition/verifier, 9 fold, and 18 nightly tests. Before merging, or after any
continuation changes, rerun exactly:

```bash
cd /Users/jackmccarthy/projects/WikiLean
git diff --check

cd wiki
npm ci
npm run test:ci
cd ..

PYTHON=.venv/bin/python3 ./scripts/ci-python.sh
```

For a quick regression while editing the current P0-R slice:

```bash
cd /Users/jackmccarthy/projects/WikiLean
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 -m unittest -v \
  brain.test_acquire_wikidata_entities \
  brain.test_fold_proposals \
  brain.test_compile_offline_pack_v2 \
  brain.test_preflight_offline_pack_v2 \
  brain.test_authority_contracts \
  brain.test_acquire_d1_snapshot \
  brain.test_harvest \
  brain.test_execution_environment \
  brain.test_prepare_replay_v2 \
  brain.test_run_replay_v2 \
  brain.test_base_graph_context \
  wiki.scripts.test_pull_annotations

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 site/ops/test_brain_nightly.py
```

Strict sandbox evidence is still outstanding. On a clean supported macOS host, require it
rather than accepting a skip:

```bash
cd /Users/jackmccarthy/projects/WikiLean
WIKILEAN_REQUIRE_REPLAY_SANDBOX=darwin \
  .venv/bin/python3 -I brain/test_replay_sandbox.py
```

Use `WIKILEAN_REQUIRE_REPLAY_SANDBOX=linux` on the intended bubblewrap host. A passing
fixture test is not a substitute for evidence under the final pinned runtime identity.

## Current corpus census

This is a local selector census against `brain/authority/reducer-inputs-v2.json`, not a
successful real source-plan preflight. No corpus payload was read or hashed for the census.

| Measure | Current value |
|---|---:|
| Declared inputs | 43 |
| Present / absent inputs | 37 / 6 |
| Required inputs present | 14 of 15 |
| Unique non-Mathlib files | 832 |
| Non-Mathlib bytes | 1,529,390,053 bytes (1.424 GiB) |
| Annotation members | 778 |
| Required input missing | `mathlib-source-tree` |

The six absent selectors are `bot-pool-candidates`, `brain-ext-anchor-links`,
`mathlib-ilean-tree`, `mathlib-source-tree`, `tauceti-links`, and `user-repos`. Five are
optional; `mathlib-source-tree` is required. The repository, external-harvest, and
declaration-oracle roots are locally available. The required external Mathlib source root
is absent on this host.

## External and authority blockers

The first real pack remains blocked on evidence or data that cannot be manufactured from
the current checkout:

1. Configure a locally held D1 Read-scoped Cloudflare token, then run and review the sealed,
   read-only annotation/community acquisition command and bind that generation's receipt/
   lineage into source-plan v3 authority. Community graduation and the annotation mirror
   already require the bundle. The current pull manifest records
   `2026-08-06T04:19:49.266Z`; D1 remains canonical and must never be re-seeded from these
   disk files. A 2026-09-05 attempt stopped before the query because Wrangler had no
   non-interactive token; no snapshot store was created and no production write occurred.
2. Restore or reacquire the read-only Mathlib source tree and bind its full commit/tree.
   Also prove the declaration oracle belongs to that exact Mathlib revision.
3. Bind the reviewed Hugging Face revisions and local consistency sidecars into canonical
   acquisition receipts/normalization lineage, and prove each multi-file dataset belongs
   to one acquisition, before treating the pins as source-pack authority.
4. Resolve the redistribution policy for `theorem_matching.csv` before publication. The
   registry text alone is not sufficient approval for pack redistribution.
5. Author the reviewed current-corpus v3 plan and supply real receipt, lineage, and request-
   preimage files for every non-Git source. The contract/compiler/preflight/replay path is
   implemented, but historical v2 receipt-like files still prove presence only.
6. Remove observation times and local paths from remaining external-harvest and
   catalog-derived bytes. Hierarchy and theoremgraph-link outputs are immutable-revision-
   derived; Frontier no longer consumes halo output, and community provenance now uses the
   sealed D1 normalization-lineage identity.
7. Proposal-fold acquisition is separated and sealed. Before issuing authority evidence,
   replace the separately fail-closed Wikidata universe/edge/description harvesters with one
   shared sealed observation generation, bind that generation plus the proposal-entity bundle
   and the reviewed Hugging Face revisions into v3 evidence, and re-harvest legacy external
   pairs through the sealed writer. Preserve the current WDQS and Action API semantics, record
   `independent-live-requests/no-snapshot`, and add a v3 inventory coherence group requiring
   universe, edges, and descriptions to name one source manifest. Resolve or bind the edge
   collector's current dependency on prior `brain/data/nodes.jsonl`. The descriptions-envelope
   consumer bug in `brain/sync_agents.py` is fixed and covered by the hermetic gate.
8. Finish the trusted OCI launcher, immutable dependency artifacts, NumPy/BLAS CPU policy,
   and strict clean-host sandbox evidence. Direct authoritative-OCI replay intentionally
   fails closed today.
9. The compiler now closes the reproduced publication, reuse, cleanup, and post-seal
   mutation races. A hostile same-UID process can still theoretically perform an ABA path
   swap during path-based content verification; eliminate that residual either with fully
   dirfd-relative verification or by running compilation in an isolated privilege boundary.

## Disk warning

The filesystem had about 43 GiB free at the final 2026-09-05 handoff check, after temporarily
falling below 6 GiB during this work. The available non-Mathlib corpus occupies 1.425 GiB,
before the required Mathlib source tree, content-addressed pack, compiler temporary
duplication, or replay outputs.

Do not infer compile readiness from the current free-space number. Run the reviewed source
plan's bounded preflight first; its estimate includes the pack, safety margin, and largest
duplicate temporary object. Prefer a larger current-user-owned `0700` pack/output store if
the exact plan does not retain ample headroom.

## Prioritized next work

1. Preserve both required hermetic gates for every continuation change; the branch now has
   independently reviewed compiler/runtime, evidence-contract, pair-publication, and fold
   fail-closed checkpoints.
2. Configure the local D1 Read credential, run and review one sealed production D1 bundle,
   bind it into source-plan authority, remove remaining observation/run metadata from
   normalized bytes, replace the three legacy Wikidata harvesters with one shared sealed
   generation, and author the reviewed current-corpus v3 source plan with immutable pins,
   licenses, receipts, lineage, the proposal-entity bundle, and the reviewed Hugging Face
   sources.
   The shared external-pair writer clock cleanup is complete. Next remove
   `concept_layer.jsonl`'s `built_at`, then the absolute checkout path in
   `mathlib_tag_xrefs.jsonl`, absolute `decl_renames.jsonl` `file_line` values, and timestamps
   in the standalone Erdos/formal-conjecture/Lean-repo/OpenAlex inputs. Add direct adapter
   cache/pagination fixtures and carry removed telemetry into the future sealed receipts.
   Do not rewrite tracked corpus files as authority without resealing them.
3. Run the bounded preflight on a larger volume; require structural success and separately
   review `compile_ready`, `source_authority_ready`, and `source_publishable`.
4. Compile and independently verify the first real pack, then prove Mathlib/oracle,
   TheoremGraph, external-pair, D1, and folded-output coherence.
5. Under the pinned runtime, perform two network-disabled builds in different paths with
   randomized mtimes/adversarial environment, compare them with the approved compatibility
   baseline, and issue the dedicated reproducibility attestation.

Only after those P0-R gates pass should release attestations switch from the v1
compatibility source identity to the verified `offline_pack_id` and `source_set_root`.
