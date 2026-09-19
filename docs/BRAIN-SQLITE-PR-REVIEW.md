# Brain SQLite PR review guide

## Review target

The former 124k-line aggregate PR is now a **15-patch dependent stack**. Start with
[#18](https://github.com/Deicyde/WikiLean/pull/18); patches 2–15 remain drafts until their
predecessor merges. The original [#17](https://github.com/Deicyde/WikiLean/pull/17) now
contains only the final CI/documentation patch, based on patch 14, so its existing discussion
is preserved without asking reviewers to use GitHub's truncated aggregate diff.

The complete stack is still available on `codex/brain-architecture-phase1` for end-to-end
verification. It is primarily contracts, verifiers, acquisition/replay tools, fixtures, and
tests; it is not a production data promotion. Every patch keeps its implementation and tests
together, and no individual patch exceeds 84 files or about 14,100 added lines.

Merge strictly in order with ordinary merge commits. After each predecessor merges, retarget
its immediate child to `main`, verify that the diff is still limited to that patch, and only
then mark the child ready. Do not squash or rebase while descendants remain open; doing so
would require rebasing every downstream branch. Keep each base branch until its child has
been retargeted.

The recommended disposition is to review and merge the **fail-closed architecture and
tooling** once the required gates and code review are green, while leaving the real source
pack, evidence approvals, baseline acceptance, attestation, and production activation as
explicit post-merge gates. The code is designed to refuse those operations when evidence is
missing.

## Exact claims

The stack claims that:

- the Brain has a deterministic indexed SQLite projection with JSONL parity and stable
  logical identities;
- generated outputs can be assembled and independently verified as one immutable,
  release-qualified closure;
- v2/v3 source and pack contracts close declared objects and evidence, and the compiler and
  preflight fail closed on missing, altered, conflicting, or undeclared inputs;
- the seven-stage candidate replay consumes an explicit prepared workspace with no live
  acquisition and enforces its sandbox, runtime, input, output, and predecessor contracts;
- acquisition/export tooling is separated from replay and has versioned independent
  verification paths;
- the two-build harness performs two real launcher executions and compares complete release
  identities, rather than accepting imported success claims;
- proposal/fold generation 2 resolves the zero-byte MIME alias conflict uniformly while
  preserving generation-1 verification and the compiler's strict global alias rule;
- the compatibility toolchain can prepare the exact old inputs, project old SQLite into the
  comparison schema, assemble releases, and support a contained legacy run; and
- the P2A authority work is an experimental shadow with no production authority effect.

Where a claim depends on fixtures, the code and docs call it fixture-scale. Historical
private evidence is not part of the Git review surface.

## Explicit nonclaims

The stack does **not** claim:

- that a complete, reviewed v3 current-corpus source plan or real full offline pack exists;
- that the private source draft or captures from the previous laptop are available here;
- that Kerodon or the replacement OpenAlex/arXiv acquisition completed;
- that all source licenses or redistribution policies have final approval;
- that the current reducer has final full-corpus runtime/sandbox evidence;
- that a seven-stage real legacy baseline, two full candidate builds, approved semantic
  comparison, or final reproducibility attestation has completed;
- that SQLite or P2A is the accepted semantic writer;
- that P1B activation evidence or the P1C rollback drill is complete; or
- that any Worker deployment, production D1 write, Wikidata submission, release promotion,
  authority cutover, or other production mutation is authorized.

The old PID and quota instructions in the 2026-09-09 handoff are historical. PID 11762 is
gone, and its private evidence root is absent on this laptop.

## Patch order

| Patch | PR | Scope | Diff |
|---:|---|---|---:|
| 01 | [#18](https://github.com/Deicyde/WikiLean/pull/18) | Immutable release artifacts and Worker reads | 44 files, +10,508/−1,046 |
| 02 | [#19](https://github.com/Deicyde/WikiLean/pull/19) | Exact release promotion and durable journal | 27 files, +10,261/−773 |
| 03 | [#20](https://github.com/Deicyde/WikiLean/pull/20) | Activation evidence bundles | 19 files, +8,438/−100 |
| 04 | [#21](https://github.com/Deicyde/WikiLean/pull/21) | Replay authority and sealed build context | 30 files, +8,761/−162 |
| 05 | [#22](https://github.com/Deicyde/WikiLean/pull/22) | Network-denied replay execution/environment | 30 files, +8,347/−468 |
| 06 | [#23](https://github.com/Deicyde/WikiLean/pull/23) | Offline-pack compiler and acquisition contracts | 43 files, +13,928/−399 |
| 07 | [#24](https://github.com/Deicyde/WikiLean/pull/24) | Sealed source-acquisition pipelines | 70 files, +10,499/−822 |
| 08 | [#25](https://github.com/Deicyde/WikiLean/pull/25) | V3 D1/Wikidata evidence and replay binding | 40 files, +11,083/−963 |
| 09 | [#26](https://github.com/Deicyde/WikiLean/pull/26) | Deterministic normalization and timestamp-only churn | 26 files, +2,149/−1,463 |
| 10 | [#27](https://github.com/Deicyde/WikiLean/pull/27) | Immutable Git/SQLite source capture | 50 files, +6,033/−327 |
| 11 | [#28](https://github.com/Deicyde/WikiLean/pull/28) | Runtime/source capture binding to replay releases | 64 files, +8,310/−174 |
| 12 | [#29](https://github.com/Deicyde/WikiLean/pull/29) | Derived, Mathlib, and MathWorld adapters | 27 files, +4,097/−18 |
| 13 | [#30](https://github.com/Deicyde/WikiLean/pull/30) | Remaining external adapters and assertion shadow | 84 files, +14,008/−26 |
| 14 | [#31](https://github.com/Deicyde/WikiLean/pull/31) | Proposal folds and exact legacy qualification | 54 files, +10,002/−59 |
| 15 | [#17](https://github.com/Deicyde/WikiLean/pull/17) | CI pins and reviewer handoff | 12 files, +488/−630 |

Patch 09 deliberately isolates most review noise: 1,376 `concept_layer.jsonl` rows change
only by removal of a nondeterministic `built_at` value. Patch 14 contains byte-exact legacy
fixtures because the comparison runner must bind the historical program bytes it executes.

Finally read [ROADMAP.md](ROADMAP.md) and the
[current handoff](BRAIN-SQLITE-HANDOFF-2026-09-19.md) to verify that implementation claims
and outstanding operational gates still match the code.

## High-risk review checklist

- A source object digest/size/media tuple has exactly one meaning across the whole pack;
  zero-byte aliases are not exempt.
- Every versioned profile reproduces its historical generation from exact program bytes.
- No verifier trusts a producer-generated success flag without recomputing closure and
  identities.
- All private and release paths are canonical, contained, non-linked where required, and
  resistant to path replacement during verification/publication.
- Candidate replay cannot read live D1, the network, undeclared repository data, or ambient
  cache/configuration inputs.
- Legacy execution cannot make reducer code or old sealed inputs writable through a parent or
  alternate bind mount; post-stage checks account for every permitted placeholder/output.
- Runtime evidence binds the exact reducer commit, launcher, Python/NumPy/SQLite closure,
  locale/hash settings, sandbox policy, and CPU-dispatch baseline.
- Release identity covers every served artifact, and Worker cursors/caches cannot cross
  releases.
- Policy and semantic approval are explicit inputs; no fixture or private qualification
  silently upgrades itself to public authority.
- Production mutation requires a separate operator action and is absent from required CI.

## Verification

Use Node 22 and Python 3.12. In a clean review worktree:

```bash
git fetch origin
git worktree add ../WikiLean-brain-review origin/codex/brain-architecture-phase1
cd ../WikiLean-brain-review/wiki
npm ci
npm run test:ci
cd ..

python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-ci.txt
PYTHON=.venv/bin/python3 ./scripts/ci-python.sh
```

At code tip `1bf1ac9f`, a fresh `npm ci` plus the named Worker gate passed 872 tests across
37 files, and the complete Python gate passed all 87 commands in 1,284.99 seconds. The hosted
Python timeout is therefore 45 minutes. The corresponding hosted Ubuntu
[run](https://github.com/Deicyde/WikiLean/actions/runs/35458917192) passed its Worker,
Python, browser, and aggregate required jobs at `faba2ca0`. The optional macOS browser soak
could not start Chromium because the host denied its Mach service registration; this was a
host launch failure, not an application assertion failure.

Also inspect, without changing frozen evidence bytes:

```bash
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
git status --short
```

`git diff --check origin/main...HEAD` has one known historical warning at
`brain/external_pair_normalization.py:58`: an intentionally retained blank line at EOF.
Eight historical normalizer profile registries pin that exact program SHA, so changing it
only for style would break retained identities or require an explicit new profile generation.
At this documentation snapshot, the worktree/new diffs are otherwise whitespace-clean.

No new private pack, capture, credential, generated public tree, or runtime image should
appear in Git. Corpus-dependent tests and real source/replay sessions require external
private data and are not fresh-checkout CI. Do not run live acquisition, deployment,
promotion, or production writes as part of ordinary PR verification.
