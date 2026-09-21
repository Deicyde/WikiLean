# Brain SQLite continuation — 2026-09-08

This is an operational checkpoint. `ROADMAP.md` remains the completion contract.
The work is on `codex/brain-architecture-phase1`, continued from `509c6f65` in
`/Users/jack/Desktop/LEAN/WikiLean-migration`. The main checkout was synchronized
to `ebac34dc`; its 670 existing data/cache changes were preserved and backed up on
`codex/local-data-backup-20260908-132955` (`ebcb58c7`). Another task may continue
committing in that original checkout; do not reset or clean it.

## Authority status

The migration is **not complete**. No reviewed full-corpus v3 source plan, real
full-corpus offline pack, two-build attestation, authoritative release, or production
activation has been produced. Production still uses compatibility authority.
No deployment or D1 write was performed during this continuation.

The first reviewed implementation tranche is committed as `f51fc27c` (source evidence,
inventory coherence, OCI tools and sealed publication fixes), followed by `3f822f04`
(native ARM OpenBLAS identity). Further acquisition and pack-bound release work has
passed integration checks and is committed as `d4ed6e42`. Completion of a fixture suite is
not evidence of a full-corpus or native Linux OCI run.

## Real source acquisitions

The sealed read-only D1 acquisition succeeded at `2026-09-08T13:41:25Z`. Independent
bundle verification confirmed 778 articles, zero community edges, and zero community
nodes, from one query. The private bundle is:

`/Users/jack/.local/share/wikilean-migration/d1-snapshots/16f5e9f905f707f9b5418d71e2d9a901262df62a2ed96b11d7d8d3e316ff5123`

- Acquisition receipt: `sha256:66cede70275acc9e9c8acee18c83c780ce1aa423729f0001504059c17a641d2a`.
- Normalization lineage: `sha256:bca17ac9b8ca6d1d6891ce7967fbcb94176c7437012f81315a32bde5acea9b5f`.
- The raw capture stays private. Existing disk annotation metadata is not authority.
- Historical acquisition tool generations must remain verifiable when current helper
  code changes. Accept whole reviewed wrapper/dependency tuples, never mixed hashes.

The independently verified private source export is
`/Users/jack/.local/share/wikilean-migration/d1-sources/b722ef778e3864aef1f5af52ae71fa145894e4515ced60b934b9e6853d3e57d3`.
Its `source-fragment.json` supplies two source entries and annotation/community
bindings for physical root `d1_export`. It retains 1,599 files/116,047,701 member
bytes, including the original capture and full seven-file implementation closure.
The parent source ID is
`sha256:784829681026a3789a337119b9d40af9ffde5fcdf45bb271264bbb228a4df96f`;
the derived source ID is
`sha256:23d534a9275b49dc04f38bcc9efe92eda350bd499ff1c43256843ecc411452bf`.
An earlier private export `ec2726ed...` is retained for audit and superseded by this one.

An exact declaration oracle and its Mathlib source revision were located through
the official [documentation build](https://github.com/leanprover-community/mathlib4_docs/actions/runs/34203087670).
GitHub artifact `10049778551` has ZIP digest
`2e1f4c6498da586d2da78da088198e79dd933b7df028bcad40f035d6efc8b17e` (117,468,491 bytes),
verified against the API artifact metadata. Its `declarations/declaration-data.bmp`
is JSON with digest `b68bb78132ed8de2f34f09fbd826ef3f5557fadc98cbf7bbb7858483f13325ac`
(67,374,686 bytes), exactly matching the existing `.claude` oracle cache.

The retained job log (`101986182527`) records Mathlib commit
`e861750b15eff0d5bc98279911ce457bb1a1382f` and doc-gen4 revision
`97d4ecdfc8e09e7f511724c25e303d448de6a3db`. The generated Topology/Basic page names
that same Mathlib commit in its source links. The source was fetched by exact commit
into a separate complete-content, shallow Git checkout; the bot checkout was untouched.
Its root tree is `3b2c28473acf9cfc3363551d91bfbc7d0ac1d2bb` and its Git archive is
110,888,960 bytes with SHA-256
`f03b69e44860cb5b91fb9166cdd54f3191a016bc467191e5345854ddf077e083`.

Private evidence locations:

- `/Users/jack/.local/share/wikilean-migration/mathlib-docs/34203087670/`:
  run/artifact API responses, job log, original ZIP, extracted oracle/page, and audit records.
- `/Users/jack/.local/share/wikilean-migration/mathlib-source/`:
  exact source checkout, Git archive, fetch log, and acquisition audit.

The original manually retained evidence above remains an audit record. A fresh,
instrumented seven-request acquisition subsequently succeeded, with real receipts,
at `/Users/jack/.local/share/wikilean-migration/mathlib-evidence-captures/0bc971fc1d1ce3f18bee753283aa77b19afc758742164545d8814f27a37fcb65`.
It reconstructs the exact 9,265-member Git tree, including 8,511 Mathlib Lean files,
and binds the documentation oracle's 424,321 declarations to that commit. The first
independently verified v3 source export is
`/Users/jack/.local/share/wikilean-migration/mathlib-source-exports/f5dfe62f5d67b6d532c1451b3064e599fe04b0e06a956c839e7e1aa793e94e1a`.
It is superseded for plan assembly by the independently verified v2 export
`/Users/jack/.local/share/wikilean-migration/mathlib-source-exports/0f599920ab77b1d4a62de7f09e68663ea4d95e6ebcffb80411943a760b62483e`.
The v2 normalizer binds a sealed complete reviewed tool-profile preimage. Its source
manifest is `sha256:720e1aac25d584afd848704e8c05f9ae99f473b2fd3eb2d6835e4e9eb77848d0`;
the docs manifest is `sha256:0912ea672c4f447c7717dfd4185c17e2319bd5a99170baa132fff6811bbd7f47`.
Keep both older evidence generations for audit; use the v2 export in the source plan.

Fresh Hugging Face acquisition and independent export verification also succeeded:

- Capture: `/Users/jack/.local/share/wikilean-migration/huggingface-evidence-captures/8356cd2318e29f5c4ac9bff91af076d17871437a08a86ee6523ef1629b5b1639`.
- Export: `/Users/jack/.local/share/wikilean-migration/huggingface-source-exports/bc180fa2ae4adc70215a0f1f90d2582c60078aaa9956ff6055f66568d01c741f`.
- Twelve actual public requests bind metadata, README files and six CSV files
  (3,102,401,461 CSV bytes) to the reviewed dataset revisions and LFS checksums.
  README Git blob hashes and publisher license declarations are retained.
- Source IDs: MathNetwork/MathlibGraph `sha256:81d936b07f3152e9fda275e031e1dca28fc95ea583770e9fbe3c6519e81a3ce3`;
  uw-math-ai/math-graph `sha256:98644aebd8499ca67335da9e3e6cab6672f11900bb2199ab067db91e9cac01d4`;
  uw-math-ai/theorem-matching `sha256:34cc31e950257b002ebb4ce205f417e948a0153973929e8b800eac015826dcfd`.
- All outputs remain private and restricted pending source-plan and redistribution
  review. The preserved acquisition profile is `sha256:98e923fea9daea19c85226b6da3df86fc5f3012a62cd7bc8ed7c359aeed82ac2`.

The first real Wikidata observation failed because WDQS appended a timeout stack trace
inside an HTTP 200 JSON response. Plan v2 uses smaller edge batches and applies the
complete pinned target scope before label lookup, preserving normalized semantics.
The optimized probe succeeded. A subsequent complete attempt failed at request 45
with an HTTP 502. Bounded private diagnostics are retained under
`wikidata-observations/failed-attempt-2770219067f94c8abbdf66c5b4629d16` beneath the same
private migration root. No failed attempt published an authority bundle or installed
partial data. Another complete attempt stopped at request 24 with an unclassified transport failure.
The transport now retains only safe numeric curl status and bounded Retry-After
diagnostics. The failed query succeeded when probed again; a larger batch was slower
and approached the upstream timeout. The next complete 194-request attempt failed
at request 69 with curl status 22/HTTP 502 and no Retry-After. Its diagnostics are
in `wikidata-observations/failed-attempt-ff538d414ceb4875a09eab8777487186`.
An explicitly versioned retry mode is being integrated: every attempt and failed
response is retained, receipt v2 counts actual attempts, and each planned request
must end in exactly one success. The existing no-retry evidence stays verifiable.
The fresh explicit-retry capture is running under profile
`sha256:e69a4fdd13c937439ba781afdec6c204f99f55387b0d361a425e3287ca0158a4`.
Its log is `wikidata-plan-20260908/acquisition-explicit-retries.log`; it has already
retained and recovered from an HTTP 502 without restarting earlier requests.

A separate complete entity capture succeeded for the 3,069 QIDs in the reviewed
observation edge/description union (covering all 2,500 grounding QIDs):
`/Users/jack/.local/share/wikilean-migration/wikidata-entity-captures/4ba24db76b9aa37c49e35cbbdd160f01a3e108c2ab8a2e208d241550e87701d6`.
The crossref derivation and its immutable source export are still being implemented.

## Engineering changes in progress

- Inventory v3 explicitly binds Wikidata universe, edges, and descriptions to one
  acquired source manifest. Preflight, compilation, and independent pack verification
  enforce this. Community-edge input becomes an immutable D1-derived source object.
  V2 inventories retain their existing acceptance and identities.
- One sealed Wikidata observation replaces separate live publication jobs. Its
  observation policy is `independent-live-requests/no-snapshot`; it makes no upstream
  transaction claim. The reviewed plan binds five selector inputs and exact count floors.
  Nightly requires an explicit plan and verifies the complete installed generation.
- The immutable D1 source exporter derives annotations solely from verified rows,
  preserving the original capture evidence. Nonempty community graduation requires
  additional explicit source dependencies; the zero-row capture must not justify a
  general empty-output fallback.
- The Mac pack publisher temporarily uses a non-searchable mode `0600` root for
  exclusive rename, reseals via its open descriptor, and rechecks full closure.
  Caught failures clean only the known candidate inode. A killed publisher may leave
  an inaccessible candidate that must be rejected rather than adopted.
- V3 source manifests can preserve distinct named outputs sharing identical CAS bytes;
  aliased digest, size, and media type must agree. V2 remains strict.
- Mathlib tag harvesting reads one immutable Git tree, emits logical paths and exact
  source/oracle hashes, and explicitly refuses to imply revision coherence from an
  unbound cache. Rename metadata normalization preserves historical reviewed claims.
- The trusted OCI launcher, image/wheel checks, numerical policy, and packaging tools
  are implemented for native Linux. Runtime v2 describes builtin `_sqlite3` linkage
  explicitly while retaining v1's extension-file contract. A real native Linux
  container kernel probe now passes under an exact scoped AppArmor policy. This is
  baseline-image evidence; the final committed runner image still needs its own probe.
- Pack-bound release production and independent verification now have a distinct
  `brain-offline-replay-v1` profile. The producer-owned two-build gate freezes all
  completed outputs plus exact sealed provenance inputs, compares bytes/identities,
  and requires an explicitly reviewed session before reproducibility attestation.
  Actual full-corpus execution is still outstanding.

## Verification environment

Use Node 22 and CPython 3.12. This Mac has an isolated development environment:

```bash
export PATH=/Users/jack/Desktop/LEAN/WikiLean-migration/.venv/bin:/Users/jack/.cache/wikilean-migration-runtime/node_modules/node/bin:/usr/bin:/bin:/opt/homebrew/bin
cd /Users/jack/Desktop/LEAN/WikiLean-migration
PYTHON=.venv/bin/python3 ./scripts/ci-python.sh
cd wiki && npm run test:ci
```

The `/usr/bin/git` ordering matters: the compiler deliberately rejects a symlinked
Git executable. The development venv includes the CI requirements and NumPy 2.3.3;
this install is not a pinned authoritative OCI dependency closure. This machine is
Darwin arm64. An isolated Linux guest has been prepared using Lima 2.2.0 and Apple's
Virtualization framework: instance `wl`, four CPUs, 8 GiB memory, 40 GiB sparse disk,
Ubuntu 24.04 arm64 in Lima plain mode, no host directory mounts or dynamic port forwarding. Its
private configuration, verified Lima archive and logs are in
`/Users/jack/.local/share/wikilean-migration/linux-runtime/`. `LIMA_HOME` for this
instance is that directory's `lima-state/`. The authoritative launcher must execute
inside the Linux guest and still prove actual container and kernel isolation.

Worker typechecking and all 872 tests passed after the new release-profile and
attestation-reference checks. Pack/compiler/preflight/publication
focused checks passed 66 tests before subsequent integration; authority contracts
passed 75 tests after the content-alias correction. Strict Darwin kernel sandbox
probing passed without skipping. The complete 55-command Python suite passed after
the new source evidence, release profile, explicit retry, and Linux policy integration;
its log is `/tmp/wikilean-migration-python-ci-native-policy.log`. The latest focused
Wikidata observation suite passes 48 cases; authority contracts pass 76. Native Linux
kernel and runtime diagnostic logs are retained in the guest's `runtime/evidence/`.
Two fresh sandboxed runtime probes agree on CPython 3.12.14, NumPy 2.3.3, SQLite 3.40.1
and bubblewrap 0.8.0; final committed-image verification is still outstanding.
Current focused release tests pass 37 cases. Strict
Darwin evidence is retained in the private migration root's
`darwin-kernel-probe-20260908.log` and is diagnostic, not OCI evidence.

## Remaining completion order

1. Finish the remaining source adapters and verify the final committed native Linux
   runner image; retain the already-passing kernel and runtime diagnostics separately.
2. Seal D1, the revision-bound oracle/Mathlib source, Hugging Face objects, shared
   Wikidata observation, proposal-fold inputs, and other source families into the
   reviewed current-corpus v3 plan. Close policy and cross-object provenance gaps.
3. Freeze actual native Linux OCI/dependency artifacts and retain strict sandbox
   evidence under that exact runtime identity.
4. Compile the real pack, run two isolated builds in different paths with adversarial
   mtimes/environment, verify complete byte/identity equality, and compare the approved
   semantic baseline with explicit provenance migration review.
5. Emit a separate reproducibility attestation and bind the verified pack/source-set
   identities into the full-offline-replay release attestation.
6. Finish the P1B public baseline/shadow/dry-run/activation evidence bundle. P1C still
   requires Jack's exact-release and window approvals for A, B, rollback, and final state.
7. Complete the roadmap's later Git assertion authority, release-pinned D1 overlay,
   and generated-artifact retirement stages. No schema or authority cutover is implied
   by completing the source-acquisition tools.
