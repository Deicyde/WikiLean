# Brain SQLite operational checkpoint — 2026-09-23

The 15-patch SQLite stack and subsequent release fixes are merged. This continuation
recovered the missing source evidence and qualified the runtime from `main` commit
`7627ccba71c1a5886b2be3f9c275e5afb836845b`. Full-corpus replay and production activation
remain unfinished. No production data, Worker deployment, or selector was changed.

## Working locations

The original Mac still has the private evidence store that was unavailable on the laptop
used for the September 19 handoff. Its old captures, drafts, and worktrees are preserved.

- Code: `/Users/jack/Desktop/LEAN/WikiLean-completion`, branch
  `codex/brain-migration-completion`, created from current remote `main`.
- New private work: `/Users/jack/.local/share/wikilean-migration/completion-20260923`.
- Retained source store: `/Users/jack/.local/share/wikilean-migration`.
- Original checkout: `/Users/jack/Desktop/LEAN/WikiLean`; still on older local `main`
  with its pre-existing data changes. Do not reset it or use its working files as
  migration authority.
- The stale `WikiLean-migration` checkout and its uncommitted September 9 work were
  not rebased, overwritten, or used as current implementation authority.

Private captures, source plans, runtime images, and raw API responses are not in Git.
Preserve the private store when moving machines; fetching this branch cannot recover it.

## Verified recovery

### Kerodon

The September 9 capture completed all 7,511 requests. Its retained transcript independently
verifies with the merged tools. New raw and normalized exports were created and separately
replayed: 7,509 page rows and 29,782 link rows match the legacy parser; the source pin changes
to identify the exact transcript. All 30 focused tests passed. No new network capture was
needed.

- New normalized export:
  `completion-20260923/kerodon/normalized-exports/816cedd6443c8f1fe3383e781775058a16ef1918f9f52a27d771be8226308466`.
- Normalized source:
  `sha256:1ad52a215069f4bd92a31f877c4f9fa4ea7a4c6d460ee1a87dc06231f782d986`.
- Exact fragments, physical roots, capture identity, verification reports, and parity
  evidence: `completion-20260923/kerodon/README.md` and `report.json`.

### OpenAlex/arXiv

A fresh capture completed 915 requests with no retries or failed attempts. It is bound to
the refreshed theoremgraph source, not the stale September 8 parent. All 21 arXiv batches
completed. The 25 citation rows are unchanged from the prior retained generation; only the
provenance pin changes. Compared with the tracked 21-row file, there are five additions and
one removal; the exact delta is retained separately.

Separate capture, raw-export, and pure-normalization verification passed. An independent
network-disabled legacy replay of all 911 successful non-documentation requests matched
the fresh normalized rows and metadata, apart from the provenance pin. All 34 focused
tests passed. The acquisition has finished; no process needs resuming.

- New normalized export:
  `completion-20260923/openalex/normalized-exports/4c47bb8aa771e89ddba5c6daf0e3deb15c17721d73b3062f49b4096f43180745`.
- Normalized source:
  `sha256:63861021c346a9bd860947530a0491f50b873596cdac53e026703cf7bdf1f662`.
- Exact raw export, receipts, roots, scope, and parity:
  `completion-20260923/openalex/README.md`, `verified-source-export.json`, and
  `legacy-parity.json`.

### D1, derived catalog, and proposal folds

The retained D1 export `315ef420…` and derived-catalog export `dda362e2…` were independently
verified again with current code. These remain the sealed September 8 inputs; this work
did not overwrite them with current live D1 state.

The earlier experimental generation-2 fold export `b513a202…` does not satisfy the final
merged profile-generation contract. Preserve it as historical evidence and use the newly
generated, independently verified export instead:

`completion-20260923/proposal-fold-exports/349546239e0973f5286eacabf61d493519d77ea1c1d5b6c06918ea40a16d11e9`.

Its profile is `sha256:f84d166245578edb3e35ed327746ac580b8d6658240ab681758befbb2b797985`.
Eleven of thirteen normalized files are byte-identical to the prior generation-1 export;
the other two contain changed source-identity metadata. The graph-input JSONL bytes are
unchanged. The five manual container contributions remain preserved, and the previously
documented 15 FC retractions and two discovery module changes remain visible decisions
for the eventual semantic review. See [the curation decision](BRAIN-SQLITE-CURATION-DECISION.md).

### Native runtime

The retained image could not simply be relabeled: current `run_replay_v2.py` differs from
the old runtime. A new native Linux image was built with networking disabled from the
retained base and NumPy wheel. The strict kernel boundary test, two actual runtime probes,
created/exited-container policy verification, and exact scoped AppArmor checks passed.
The current native legacy diagnostic also passed all 13 checks. That diagnostic is a
fixture, not the seven-stage full-corpus baseline.

- OCI manifest:
  `sha256:501e6b65d365e292ad6a660176a99121080390a2fe1a4d7c2f6e53e81a5753ea`.
- Environment:
  `sha256:4630a2d5e4fbf01d2f60d7a0052c46e9c6cc201ec91de0b29912b1955b328d77`.
- Descriptor:
  `linux-runtime/qualification-7627ccba-20260923/guest-reports/execution-environment-7627ccba.json`.
- Full commands, runtime observations, identities, and report:
  `linux-runtime/qualification-7627ccba-20260923/README.md` and `qualification-report.json`.

This qualifies the runtime for the recorded code commit. It does not certify a full pack
or a full replay, and it must be rebound and rechecked if the final authority commit changes.

The dedicated `wl` VM was already running at session start. After confirming it had no
containers or active user jobs, it was stopped gracefully to release its 8 GiB memory
reservation. Its configuration, disk, image, and qualification evidence remain retained;
`completion-20260923/runtime-shutdown-report.json` records the independently observed
`Stopped` state. The private qualification README has the exact restart command.
This recovered storage headroom temporarily; check current host capacity before restarting.

## Source-plan integration

The new private assembler selects exactly eight source replacements and eight additions,
preserves all other source entries and curated Git pins, uses the folded inventory, and
closes the two external aggregate input groups with Kerodon. Verification passed for
114 sources and all 43 input groups: 84 acquisition receipts, 108 normalization lineages,
and 9,944 request-parameter preimages. It excludes the two unbound fold-comparison
diagnostic sources. Source restrictions remain intact. The initial plan SHA-256 is
`26977c772b009c7de3b3155169cc12acd15d404df55072f60ee0c3e68b3235e1`;
its 80,133 unique objects total 8,057,483,795 bytes.

Use the corrected compiler candidate at `candidate-03/source-plan.json` with
`candidate-03/roots.json`, not either earlier plan. Preflight caught one curated
registry binding whose physical root label disagreed with the inventory. The correction
changes only that label from `wikilean_git` to `repo`; the original registry Git pin,
bytes, all 114 source-manifest identities, input bindings, and evidence remain unchanged.
`candidate-02/root-binding-correction.json` retains the exact derivation and the Git-byte
check at the new root.

A targeted control check then found that all 23 schema documents were valid but retained
pretty-printed Git bytes, while preflight requires canonical JSON. `candidate-03` stores
new canonical copies in a private `schema_control` root: 91,278 bytes with identical parsed
documents. Only their file-reference roots, sizes, and hashes change from `candidate-02`;
every source entry and logical input binding is identical. The current plan SHA-256 is
`5f601da7fce3bb9997e70a5a29149bfc17444a70923045de0b5c587743ae1e71`.
`candidate-03/schema-canonicalization.json` records the exact transformation. The earlier
candidates and unsuccessful preflight attempts remain available for diagnosis.
The independent logical-input check covers all 43 groups and 9,345 members, including
exact curated Git blobs and path-collision checks; see
`candidate-02/logical-input-independent-review.json`. Those bindings are unchanged in
`candidate-03`; its `logical-input-review-applicability.json` binds that correspondence.
`candidate-03/control-independent-review.json` independently verifies all 23 canonical
schemas plus the unchanged configuration/environment controls and exact runner commit.

Private control files are `candidate-config.json`, `assemble-candidate.py`, and
`stage-candidate.py`. Both scripts received an independent read-through. They create new
outputs and leave the old draft and physical source roots untouched. The staging worktree
uses the original c644 Git pin for curated selectors; the reducer has a separate current
Git root. Staging completed 833 logical input files totaling 1,448,569,214 bytes.
Non-Git inputs use native APFS `clonefile` with no full-copy fallback; the copies have
separate inodes and verified SHA/size. An initial report-writing import failure occurred
after copying; a read-only verification pass then checked the complete staged inputs and
wrote `candidate/roots.json` and `candidate/staging-report.json`. No copies were repeated.
The independent `candidate/staging-independent-review.json` subsequently verified every
input and schema against its origin, the exact file/directory closure, and absence of
symlinks, special files, and hard links. Both staging-script review findings were resolved.

Assembly/staging results are under `candidate`; corrections are under `candidate-02` and
`candidate-03`. The final readiness invocation and result are `preflight-invocation.json`,
`preflight.json`, `preflight.stderr`, and `preflight-exit.json` in the run root. A complete
source plan is not a compiled pack, public-policy approval, or accepted authority.

The initial normative preflight completed with `ok: true` and exit 2, which reports remaining
readiness concerns rather than a validation failure. All 15 required inputs are present;
the 43 groups contain 38 present and five explicitly absent optional groups, totaling
9,345 members. All v3 receipt/lineage/preimage and parent-closure checks passed again.
`compile_ready`, `source_authority_ready`, and `source_publishable` are all false.
The sole compilation blocker is insufficient storage: 1,823,653,888 bytes free against a
9,303,867,452-byte estimated peak and 10,699,447,570 bytes recommended with headroom.
The estimated pack alone is 8,249,835,136 bytes. These estimates exclude subsequent replay
workspaces and outputs. The 325 warnings comprise 97 native pins not locally independently
verifiable and 114 source-level plus 114 object-level restricted-publication notices.
Corpus payloads were size-checked here; compilation must still hash their full bytes.

After the idle VM was stopped, a separate fresh preflight completed successfully:
`preflight-capacity-restored.json` reports `ok: true`, `compile_ready: true`, and zero
compilation blockers, with 16,614,936,576 free bytes at its capacity sample. The source
authority/publication warnings are unchanged. Its invocation and stderr are retained
separately; the earlier resource failure remains historical evidence. Full-pack compilation
then started under a resource supervisor with a 3 GiB host reserve. The supervisor records
capacity in `full-pack-resource-log.jsonl` and will independently verify the compiler's
actual resulting manifest. Keep the VM stopped while compilation runs.

The legacy preparation recipe was also checked without copying its inputs. The retained
checkout still has the exact required `ebac34dc1d07b66ce97692c31a914a084328f5df`
commit/tree and all eleven old program/support files. Its 9,345 input members, mixed-data
overlays, and programs require at least 1,616,143,663 copied bytes, excluding outputs,
the completed-layout copy, SQLite, release assembly, guest pack transfer, and host swap.
See `legacy-preparation-plan.json`. No old full-corpus stage was executed.

The independent source-policy review covers exactly these 114 sources across 19 families,
with 161 retained evidence files checked. All decisions remain pending. The review matrix
separates private raw-input retention from prospective public fields identified in normalized
inputs and renderer code; exact release-artifact review remains outstanding. Concrete
scope questions include DLMF bulk-content terms, Kerodon raw-content permission, excluded
ProofWiki namespaces retained in the raw dump, conflicting PlanetMath and TheoremGraph
license statements, and public excerpt/code attribution. See
`completion-20260923/source-policy/README.md`, `review-matrix.json`, and `validation.json`.
`validation-candidate-02.json` and `validation-candidate-03.json` prove correspondence
with the two corrected plans; all 114 source identities remain covered.
These are questions for the operator's standalone private/public policy reviews, not
approved policy changes. Public-policy readiness is not currently enforced by the promoter.

## Public assets and operator access

All three search-index families were generated offline with the exact current committed
generators and verified Node 22.23.2, from pinned declaration and MathNetwork inputs:

| Index | Verified result |
|---|---|
| Declarations | 424,321 declarations, 879 shards |
| Suffixes | 255,533 buckets, 1,289 shards |
| Premises | 273,508 sources, 1,046,885 edges, 175,445 names; 497 shards and 22 name chunks |

The 2,690 files total 92,429,392 bytes. The independent index-closure verifier, shard counts,
name references, source hashes, and generator hashes pass. Keep these timestamp-bearing
bytes unchanged for eventual baseline assembly; see
`completion-20260923/public-index-preparation/README.md` and `result.json`.

The optional D1 mode in `export_wikidata_rdf.py` now takes explicit article-object and
annotation/catalog/output paths plus the expected D1 digest. It validates canonical rows,
safe unique membership, and every exact sidecar before rendering from captured values.
Default legacy behavior remains unchanged. Twelve focused tests passed, including exact
legacy byte parity and independence from ignored rendered article files; a separate review
found no actionable defect. The test is registered in the offline Python gate.

A complete non-Brain tree has been generated from those verified D1 rows, the pinned
catalog, current shell generators/assets, and the preserved search indexes. It contains
2,699 files totaling 96,337,594 bytes. The concepts page has 631 concepts and 8,937
declaration links; the annotation-derived autocomplete index has 5,380 pairs. Independent
review checked every file, the unchanged required-payload/index-closure contracts, all
3,476 clone correspondences and separate inodes, generator/helper bytes, and input lineage.
All 2,690 prepared search-index files remain byte-identical. Exact commands and reports are
under `completion-20260923/public-asset-preparation`.

The canonical inventory is now prepared at `wiki/public-asset-source-attestation.json`:
351,949 bytes, SHA-256 `e3a2de02bee45054d17da334b8f8e4424b624024b048dd96cfb5f8e1e54172cd`.
A separate read checked all inventory hashes against the actual tree. This is the concrete
inventory for review; it is not a frozen baseline, source-policy approval, or a production
authority commit. Preserve the exact private tree until reviewed main commit C is known.

Worker typecheck and all 885 tests across 38 files passed with verified Node 22.23.2.
The first full Python run stopped at existing compiler tests because the host's default
Git path is a symlink. The gate is being rerun with the literal verified Git executable
and Node 22 first on `PATH`; no test or compiler contract was weakened. Both full logs
are retained as `python-ci-completion.log` and `python-ci-completion-verified-tools.log`.

A real
bootstrap Brain release is **not** required by the baseline contract: only the monolithic
`build-public.ts` convenience path requires one. Direct assembly must still pass the
unchanged required-payload and index-closure checks. The retained synthetic Brain release
is a format fixture and supplies no real-release evidence.

Fresh locked dependencies were installed in the completion checkout. Read-only Wrangler
4.120.0 `deployments status`, `deployments list`, and `versions list` all succeeded with
the verified private Node 22.23.2 binary. This proves read access, not deployment approval
or write permissions. Exact tool and response evidence is under
`completion-20260923/cloudflare-preflight`.

Production still serves the August 28 Brain snapshot. The release selector returned HTTP
404 on September 23, while the API returned the August 28 snapshot with no release ID;
the responses and hashes are retained in `completion-20260923/live-observation`. The
first real promotion therefore needs the runbook's explicit
first-deployment handling. No production mutation was attempted.

## Immediate continuation

1. Provide storage headroom for the replay sequence. Free space initially fell to 1.7 GiB;
   stopping the idle VM recovered enough for a fresh passing preflight and compilation.
   The assembled plan alone references about 7.5 GiB of distinct objects; compilation, prepared
   input copies, full graph outputs, releases, and VM storage all need additional space.
   The user was asked to free at least 30 GiB or provide an external storage path. Do not
   treat the guest VM's reported free space as separate from the host backing its disk.
2. Finish compilation and independent verification of `candidate-03` under the resource
   supervisor, then record the first full pack's actual IDs. Preserve
   restricted source policies; source-plan or fixture success is not publication approval.
3. Review the concrete private-use policy evidence and source/decision deltas. Run the
   exact seven-stage legacy baseline and the two isolated candidate builds. Verify semantic
   parity and provenance coverage, then retain the appropriately reviewed attestation.
4. Review and land the prepared non-Brain asset inventory to establish final authority
   commit C, then freeze the unchanged private public tree against C. Rebind the
   runtime/candidate to C as required; a technical result
   bound to 7627ccba is not an activation result bound to C.
5. Follow [the release runbook](BRAIN-RELEASE-RUNBOOK.md) for the retained dry run, activation
   evidence, concrete release review, production promotion, canaries, and rollback exercise.
   `completion-20260923/release-prerequisite-audit.json` lists the observed prerequisites.

P0-R and P1 activation remain open. Later Git-authority, D1-overlay, and generated-artifact
retirement phases retain their separate [roadmap](ROADMAP.md) boundaries.
