# Brain SQLite operational handoff — 2026-09-05

This is a branch-state and operator handoff, not the project plan. The canonical plan and
completion criteria remain in [`ROADMAP.md`](ROADMAP.md), especially P0-R and P1A–P1C.
Architecture details live in [`BRAIN-ARCHITECTURE.plan.md`](BRAIN-ARCHITECTURE.plan.md), and
the executable authority contracts are summarized in
[`../brain/authority/README.md`](../brain/authority/README.md).

## Bottom line

The SQLite architecture and its fixture-scale sealed replay path are substantially
implemented. This branch does **not** yet establish an authoritative full-corpus build:

- no reviewed current-corpus source plan exists;
- no real `offline-pack/v2` has been compiled;
- no full-corpus clean-room replay, dual-build equality result, or reproducibility
  attestation exists;
- no v2 pack identity has been bound into an authoritative release; and
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
- versioned source-manifest, `offline-pack/v2`, reducer-inventory, build-context,
  execution-environment, and build-attestation contracts;
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
- a canonical offline-pack source-plan schema and deterministic, network-free v2 compiler;
- a bounded source-plan preflight that hashes and validates only small control documents,
  uses Git/lstat sizes for corpus payloads, and reports separate `compile_ready`,
  `source_authority_ready`, and `source_publishable` states; and
- source-selector and test-gate updates for the current reducer inventory.

The report is explicitly `source-plan-only` with `runtime_environment_checked: false`; its
readiness fields are not runtime, replay, release, or deployment claims. The preflight CLI
returns `0` only when `source_publishable` is true, `2` for a valid but non-publishable source
plan (including warning-class authority/publication concerns), and `1` for structural or
argument errors. All report/error output is canonical JSON. Receipt-role detection is
presence-only in v2: standalone `acquisition-receipt/v1` and
`normalization-lineage/v1` contracts now exist, but they cannot make a v2 source
authority-ready until an explicit v3 pack/source-manifest integration seals and verifies
their bytes.

The subsequent acquisition-integrity milestone adds:

- a shared `external-pair/v1` semantic identity and strict reader validation for newly
  emitted external page/link artifacts;
- per-database writer locking, canonical serialization, a durable transaction journal,
  hard-linked prior-generation recovery, and explicit zero-link partners, with coverage for
  caught failures, first-publication interruption, concurrent writers, and real `SIGKILL`;
- validation in the graph build, proposal fold, agent candidate generator, and acceptance
  gate, while explicit replay bindings reject orphan or unregistered source objects; and
- fail-closed inline Wikidata acquisition in `fold_proposals.py`, including typed response
  validation, redirect support, missing-QID isolation, complete-batch enforcement, and a
  proof that later-batch failure leaves every prior output byte unchanged.

This does not complete the acquisition/replay split: current legacy pairs remain readable,
Wikidata is still fetched inside the fold command, and physical external JSONL bytes still
contain observation/run metadata even though that metadata is excluded from
`pair_generation`.

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

These pins do not by themselves make v2 authority-ready: acquisition receipts, lineage,
and request preimages still require explicit v3 source-plan/source-manifest/offline-pack
integration.

The sealed D1 acquisition foundation additionally exists, but has not been run against
production in this branch. `brain/acquire_d1_snapshot.py` uses one checked-in read-only
statement for articles, every community edge (including tombstones), and community nodes;
checks row counts and exact migrated column inventories; binds the production account and
database UUID plus the pinned Node/Wrangler closure; and atomically publishes a private,
content-addressed bundle containing clock-free normalized objects and validated receipt/
lineage evidence. No source-plan or release authority claim follows from the acquisition
tool alone.

Community graduation is now wired to that boundary: `harvest_community_edges.py` accepts
only an explicit sealed bundle, independently re-verifies its closure, evidence, normalized
rows, and tombstones, and pins output provenance to the normalization-lineage identity. It
has no live-query or fixture bypass. The annotation mirror and source-plan authority are
still not wired to the bundle, and no production bundle has been captured.

Nightly operations are portable across checkouts. `site/ops/nightly-launchd.py` validates a
sparse launchd-like environment, seals the exact checked Python and Mathlib paths into
generated plists, and installs files without loading jobs. Community graduation is off by
default and requires an absolute reviewed bundle path; the moderation job never acquires D1
state implicitly.

Frontier replay no longer accepts `manage/data/halo.json` as authority input.
`mean_stateability` is deterministically re-derived from the exact bound cells and synapses
with the historical ring-1 neighbor-fraction semantics. On the current corpus, all 47 area
IDs, membership, proximity, suitability, ordering, and top-cell choices are unchanged; 22
stateability summaries, the stateability input-count metadata field, and the reducer
inventory identity changed. The operational halo report remains available for management
worklists only.

## Verification state and required final commands

Focused results recorded through 2026-09-05:

- offline-pack compiler: 22 tests passed;
- source-plan preflight: 15 tests passed;
- authority contracts: 65 tests passed;
- proposal-fold acquisition: 10 tests passed;
- external pair publication/reader fixtures: all checks passed;
- execution environment: 20 tests passed;
- replay executor: 33 tests passed;
- replay preparation: 24 tests passed;
- base-graph context: 11 tests passed after the annotation-selector fix; and
- replay sandbox: one expected local skip because strict clean-host evidence was not
  requested/available.

The latest continuation additionally passed 11 sealed-harvester tests, 8 D1-acquisition
tests, 10 top-level-shard tests, 15 portable-launcher tests, and 13 Brain-nightly shell
tests. The launcher/harvester integration received an independent clean P0/P1 audit.

The acquisition checkpoint passed the full Python gate (36 commands) and the Worker gate
(37 files / 845 tests). Before merging, or after any continuation changes, rerun exactly:

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
  brain.test_compile_offline_pack_v2 \
  brain.test_preflight_offline_pack_v2 \
  brain.test_authority_contracts \
  brain.test_acquire_d1_snapshot \
  brain.test_execution_environment \
  brain.test_prepare_replay_v2 \
  brain.test_run_replay_v2 \
  brain.test_base_graph_context
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

1. Run and review the new sealed, read-only D1 annotation/community acquisition command,
   then migrate the annotation mirror and source-plan authority to its one-generation
   bundle. Community graduation already requires that bundle. The current pull manifest
   records `2026-08-06T04:19:49.266Z`; D1 remains canonical and must never be re-seeded
   from these disk files. No production D1 query was run while implementing the tool.
2. Restore or reacquire the read-only Mathlib source tree and bind its full commit/tree.
   Also prove the declaration oracle belongs to that exact Mathlib revision.
3. Bind the reviewed Hugging Face revisions and local consistency sidecars into canonical
   acquisition receipts/normalization lineage, and prove each multi-file dataset belongs
   to one acquisition, before treating the pins as source-pack authority.
4. Resolve the redistribution policy for `theorem_matching.csv` before publication. The
   registry text alone is not sufficient approval for pack redistribution.
5. Integrate the standalone acquisition-receipt and normalization-lineage contracts through
   explicit v3 source-manifest/source-plan/offline-pack contracts, including sealed
   request-parameter preimages. Current v2 receipt-like files still prove presence only,
   not complete batch success or output ancestry.
6. Remove observation times and local paths from remaining external-harvest and
   catalog-derived bytes. Hierarchy and theoremgraph-link outputs are immutable-revision-
   derived; Frontier no longer consumes halo output, and community provenance now uses the
   sealed D1 normalization-lineage identity.
7. Finish acquisition separation before issuing evidence: move the now-fail-closed
   Wikidata lookup out of `fold_proposals.py`, make the remaining
   Wikidata harvesters reject partial results, and bind the reviewed Hugging Face revisions
   into v3 evidence. Re-harvest legacy external pairs through the sealed writer.
8. Finish the trusted OCI launcher, immutable dependency artifacts, NumPy/BLAS CPU policy,
   and strict clean-host sandbox evidence. Direct authoritative-OCI replay intentionally
   fails closed today.
9. The compiler now closes the reproduced publication, reuse, cleanup, and post-seal
   mutation races. A hostile same-UID process can still theoretically perform an ABA path
   swap during path-based content verification; eliminate that residual either with fully
   dirfd-relative verification or by running compilation in an isolated privilege boundary.

## Disk warning

The filesystem had about 6.3 GiB free at the latest check, after fluctuating as low as
1.2–3.0 GiB during this work.
The available non-Mathlib corpus already occupies 1.425 GiB, before adding the required
Mathlib source tree, the content-addressed pack, compiler temporary duplication, or replay
outputs. This is not safe headroom for the first real pack.

Do not start full compilation on this volume. Move the private pack/output stores to a
larger current-user-owned `0700` filesystem, then use the reviewed source plan's preflight
space report. Its estimate includes the pack, safety margin, and the compiler's largest
duplicate temporary object; the real plan is required for an exact number.

## Prioritized next work

1. Preserve both required hermetic gates for every continuation change; the branch now has
   independently reviewed compiler/runtime, evidence-contract, pair-publication, and fold
   fail-closed checkpoints.
2. Run and review one sealed production D1 bundle, migrate the annotation and source-plan
   consumers, remove remaining observation/run metadata from normalized bytes, finish
   Wikidata acquisition separation, then design the explicit
   v3 receipt/lineage pack integration
   (including the reviewed Hugging Face sources) and author the
   reviewed current-corpus source plan with immutable pins, licenses, receipts, and lineage.
3. Run the bounded preflight on a larger volume; require structural success and separately
   review `compile_ready`, `source_authority_ready`, and `source_publishable`.
4. Compile and independently verify the first real pack, then prove Mathlib/oracle,
   TheoremGraph, external-pair, D1, and folded-output coherence.
5. Under the pinned runtime, perform two network-disabled builds in different paths with
   randomized mtimes/adversarial environment, compare them with the approved compatibility
   baseline, and issue the dedicated reproducibility attestation.

Only after those P0-R gates pass should release attestations switch from the v1
compatibility source identity to the verified `offline_pack_id` and `source_set_root`.
