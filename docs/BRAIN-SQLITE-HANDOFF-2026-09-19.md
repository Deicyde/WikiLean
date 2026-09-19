# Brain SQLite handoff — 2026-09-19

This is the current laptop-transfer checkpoint for the 15-patch review stack ending at
`codex/brain-architecture-phase1`. The durable completion contract is
[ROADMAP.md](ROADMAP.md), especially P0-R. The review map and exact claims are in
[BRAIN-SQLITE-PR-REVIEW.md](BRAIN-SQLITE-PR-REVIEW.md).

## Bottom line

The branch is suitable for reviewer attention as a **fail-closed tooling change**. It
implements the SQLite projection, immutable release machinery, sealed source/pack contracts,
offline replay boundary, acquisition verifiers, reproducibility gate, comparison tooling,
and experimental assertion shadow. Those systems refuse missing or inconsistent authority
evidence; they do not manufacture it.

The migration is not operationally complete. There is no reviewed current-corpus v3 source
plan, real full pack, approved legacy semantic baseline, two-build full-corpus result, final
reproducibility attestation, or production activation. Nothing in this branch authorizes a
deployment or production write.

## Current checkout and machine state

- Repository: `/Users/jackmccarthy/projects/WikiLean`
- Branch: `codex/brain-architecture-phase1`
- Remote: `origin` = `Deicyde/WikiLean`
- The code checkpoint is 61 commits ahead of `origin/main`; use `git log` for the exact
  current tip because final review fixes may add commits after this document.
- Review starts at [PR #18](https://github.com/Deicyde/WikiLean/pull/18). PRs
  [#19–#31](https://github.com/Deicyde/WikiLean/pulls) and the retargeted final
  [PR #17](https://github.com/Deicyde/WikiLean/pull/17) form a strict dependent stack; see
  the review guide for exact order, sizes, and merge protocol.
- The previous private evidence root,
  `/Users/jack/.local/share/wikilean-migration`, is absent on this laptop. No equivalent
  private store or required external Mathlib checkout was found locally. Private captures
  described in the 2026-09-08/09 documents therefore cannot be treated as available here.
- The old Kerodon capture PID 11762 no longer exists. Do not try to resume or signal it and
  do not follow the old live-process instructions.
- No production deploy, release promotion, D1 write, Wikidata submission, or authority
  cutover was performed in this continuation.

The 2026-09-09 handoff is historical evidence of what existed on the previous machine. It
reported a private draft with 106 manifests and 41 of 43 input groups, retained native-Linux
runtime evidence, a partial OpenAlex/arXiv capture, and an in-progress Kerodon capture. The
Kerodon outcome is unknown. None of those private bytes are in Git, and their continued
existence has not been established on this laptop.

To resume from another machine in a fresh clone:

```bash
git clone https://github.com/Deicyde/WikiLean.git
cd WikiLean
git switch --track origin/codex/brain-architecture-phase1
git status --short --branch
```

For an existing clone, fetch `origin`, switch to `codex/brain-architecture-phase1`, and use
`git pull --ff-only origin codex/brain-architecture-phase1`. The tracked branch contains all
reviewable implementation and documentation; it intentionally does not contain private
captures, credentials, runtime images, or a full offline pack.

## Implemented state

### Merge-reviewable, fail-closed tooling

- Deterministic schema-v2 SQLite projection, JSONL parity checks, stable logical/raw
  identities, persisted planner statistics, atomic publication, and bounded query probes.
- Immutable Brain releases that close over graph, SQLite, cells, frontier, shards, traces,
  xrefs, and the Brain page; release-qualified Worker reads and independent streamed closure
  verification.
- Versioned source-manifest, source-plan, offline-pack, reducer-inventory,
  execution-environment, build-attestation, and reproducibility-attestation contracts.
- A deterministic, network-free pack compiler and bounded source-plan preflight with
  separate structural, authority, and publication readiness results.
- Copy-only workspace preparation and a seven-stage replay entry point with explicit inputs,
  output ownership, predecessor integrity checks, network denial, runtime verification, and
  fail-closed sandbox selection.
- A real two-launch reproducibility gate that randomizes paths, mtimes, cache/temp roots, and
  ambient `BRAIN_*` settings. The gate is tested; it has not yet run twice on a real full pack.
- Sealed acquisition and independent verification tooling for D1, Wikidata proposal entities,
  shared Wikidata observations, Hugging Face inputs, and the other source families described
  in the roadmap. Historical private captures are not bundled with the PR.
- Proposal/fold export generation 2 canonicalizes every zero-byte object as
  `application/octet-stream`, eliminating the empty NDJSON/octet-stream alias conflict
  without weakening the compiler's global alias rule. Generation 1 remains explicitly
  replayable; real private exports must be regenerated and rebound to a reviewed plan.
- Same-input legacy preparation, schema-1-to-schema-2 compatibility projection, legacy
  release assembly, semantic comparison, and diagnostic execution support. Independent
  review fixed both the read-only-parent and nested-bind `EXDEV` failures, pins the exact old
  tree/programs/argv, overlays inputs from a disjoint verified source, and closes every
  permitted Brain output. Its 53-test focused family and alternate-hash-seed driver/assembler
  runs pass. A real seven-stage native Linux/full-pack result remains outstanding.
- The P2A assertion kernel is implemented only as an experimental shadow. It changes no
  accepted authority or production route.
- CI, D1 backup, and Wikidata-poller workflows use exact current Node-24 action commits and
  Node 22 project toolchains. The required Python job has a 45-minute limit for its expanded
  87-command suite; required aggregation remains Worker plus Python only.

### Validation snapshot

At code tip `faba2ca0`, both required local gates passed cleanly after all implementation
changes:

- fresh `npm ci` plus `npm run test:ci`: **37 files / 872 tests**, including typecheck;
- `PYTHON=.venv/bin/python3 ./scripts/ci-python.sh`: **87 commands**, all offline scenarios,
  in **1,284.99 seconds** wall time; and
- the legacy focused family: **53 tests**, plus alternate-hash-seed driver/assembler reruns.

The longer Python measurement is why hosted CI now has a 45-minute timeout. Reviewers can
reproduce the named gates with:

```bash
cd wiki
npm ci
npm run test:ci
cd ..

PYTHON=.venv/bin/python3 ./scripts/ci-python.sh
```

Do not describe fixture success as a real full-corpus replay or reproducibility result.

The optional local Playwright soak could not launch Chromium on this managed macOS host:
Chromium failed before page creation with a Mach service `Permission denied` error. This is
not an application assertion failure. The equivalent non-required hosted Ubuntu browser job,
both required hosted jobs, and their aggregate gate passed at `faba2ca0`.

## Remaining blockers

### Evidence and source approval

1. Recover the previous private evidence store from an intentional backup or reacquire each
   source into a new immutable generation. Never reconstruct receipts from normalized files
   or silently relabel an incomplete capture.
2. Recover and verify a completed Kerodon result, or reacquire it if the old in-progress
   capture did not survive. The old OpenAlex run stopped on an arXiv 429 and likewise needs a
   complete retained replacement. Any new run needs its own transcript, receipt, lineage,
   and independent verification.
3. Restore a separate read-only Mathlib checkout at the exact reviewed revision, regenerate
   portable `mathlib_tag_xrefs` and `decl_renames` metadata, and bind the declaration oracle
   to that tree. Never edit the bot checkout.
4. Regenerate the proposal/fold source exports with generation 2 and update every dependent
   source reference. Preserve and continue to verify historical generation-1 exports.
5. Author and review one complete v3 current-corpus source plan. It must close D1,
   Mathlib/oracle, shared Wikidata and proposal entities, Hugging Face, external database,
   curated Git, derived, and fold inputs, with concrete license/redistribution decisions.

### Full-corpus proof

6. Finalize the reducer/legacy-driver commit and bind it to a newly verified execution
   environment. Retain strict native sandbox/runtime evidence under that exact identity.
7. Compile and verify the first real full pack, prove cross-object/source-revision coherence,
   and retain its exact `offline_pack_id` and `source_set_root`.
8. Run the seven-stage legacy baseline and assemble the approved comparison release. Review
   any provenance-only normalization separately from graph/content semantics.
9. Run the two real isolated candidate builds, require byte/root/release equality, compare
   them with the approved legacy baseline, and finalize the separately reviewed
   reproducibility attestation.

### Human and production gates

10. Obtain explicit review for source policy, semantic-baseline acceptance, and the final
    attestation. A passing private qualification report is not public redistribution approval.
11. P1B activation evidence and P1C rollback/window approval remain separate. Production
    promotion requires Jack's explicit approval from a clean merged `main`; nightly remains
    shadow-only and must not deploy.

## Recommended continuation order

1. Review the stack from PR #18 through final PR #17 using the companion guide. Merge only
   bottom-up with merge commits, retargeting each immediate child to `main` after its parent
   lands; do not squash/rebase while descendants remain open.
2. In parallel, locate the prior private store. If it cannot be recovered, write a new
   acquisition plan rather than relying on historical paths or process state.
3. Close Kerodon, OpenAlex/arXiv, Mathlib/oracle, fold-generation-2, and tracked portable
   metadata evidence; then review the concrete licenses and v3 source plan.
4. Reseal the final reducer/runtime identity, compile the real pack, and run the legacy and
   dual-build comparison session.
5. Review and retain the final attestation. Only then schedule separately authorized
   activation and rollback work.

This order lets reviewers evaluate the safety and determinism of the tooling without
pretending that private evidence, source approvals, or production readiness are part of the
PR.
