# Brain SQLite PR review guide

## Review target

PR: [#17](https://github.com/Deicyde/WikiLean/pull/17), branch
`codex/brain-architecture-phase1` against `origin/main`.

At code checkpoint `1bf1ac9f`, the branch contained 58 commits touching 369 files
(about 124,169 insertions and 4,545 deletions). This guide and the current handoff are added
after that snapshot, so use `git diff --stat origin/main...HEAD` for final totals. The size is
primarily contracts, verifiers, acquisition/replay tools, fixtures, and tests. It is not a
production data promotion.

GitHub's aggregate diff endpoint rejects this change as `too_large` above 300 files. Review
the bounded commit clusters below or use the clean worktree commands in this guide; do not
assume the web UI's aggregate file view is complete.

The recommended disposition is to review and merge the **fail-closed architecture and
tooling** once the required gates and code review are green, while leaving the real source
pack, evidence approvals, baseline acceptance, attestation, and production activation as
explicit post-merge gates. The code is designed to refuse those operations when evidence is
missing.

## Exact claims

This PR claims that:

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

This PR does **not** claim:

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

## Commit-cluster review order

The history is intentionally incremental. Review by boundary rather than reading all 55+
commits strictly chronologically.

1. **Authority contracts and identity primitives**

   Start with `05205bea` (replay authority contracts), `196bfc31` (acquisition evidence),
   `dba596f2` (v3 evidence contracts), `f5a2c9c9` (pack compiler), and the schema/verifier
   tests. Check canonical encoding, domain separation, logical-versus-audit identity, closure,
   version compatibility, path containment, and streaming bounds first.

2. **Build context and offline execution boundary**

   Review `e56b7ce1`, `99cc8f57`, `1e18462a`, `b23cff32`, `d7ad0faf`, `14a0020f`,
   `bc158cfd`, and `85d30e54`. Trace one fixture from verified pack through preparation and
   all seven stages. Focus on exact input binding, read-only code/data, scratch/output
   ownership, network denial, runtime probing, mutation detection, and no ambient path/mtime
   identity.

3. **Source acquisition and normalization boundaries**

   Review `e07e1a4f`, `8db5203f`, `581a70f1`, `61f49b2b`, `92703ac0`, `7d311adb`,
   `4cd402c1`, `39632d33`, `03129828`, then the later source-family clusters from
   `a99d48fa` through `c3265f80`. Check that network access ends at a sealed bundle, evidence
   generations cannot overwrite each other, request sets are complete, verifier code is
   independent, and historical profiles remain replayable.

4. **Deterministic reducer and source cleanup**

   Review `c3d94243`, `8996c194`, `7b2909da`, `49ad4bb7`, `fc052f7c`, and `f5373f26`.
   Confirm that removed clocks, mtimes, cache counters, absolute paths, and moving-worktree
   reads are replaced by explicit pins/evidence rather than merely omitted.

5. **Private-export adapters, fold closure, and policy gates**

   Review `dded5b39`, `bb179a0c`, `1bd15ac3`, `69cfb173`, and `a2001a6c`. Pay special
   attention to proposal/fold generation-1 compatibility, generation-2 zero-byte media
   canonicalization, complete source-parent closure, preserved curation deltas, and the
   separation between private qualification and public policy approval.

6. **Legacy semantic comparison and reproducibility session tooling**

   Review `945f9c81`, `952eac23`, `8928d7c7`, and `0d14f93a`. Verify exact old program/tree
   hashes, seven-stage recipe and argv, writable-path
   containment across bind mounts, complete launch evidence, the schema-1 compatibility
   projection, release assembly, and the rule that only an explicitly reviewed real session
   can finalize an attestation.

7. **Immutable release and production boundary**

   Review `75e1bbf2`, `6fbc59e9`, and `72943e68`, plus Worker release tests. Confirm that
   public reads are release-qualified, aliases are byte-identical, promotion consumes an
   exact already-built release, dry-run does not mutate production, rollback remains explicit,
   and no nightly path deploys.

8. **Experimental authority shadow and integration**

   Review `f670cbf3` last. It should remain bounded, deterministic, and disconnected from
   accepted authority and production routes.

9. **CI/runtime maintenance**

   Review `1bf1ac9f` independently of the architecture. It pins every official action to a
   current immutable Node-24 release commit, keeps project jobs on Node 22, preserves the
   workflow permission boundaries, and raises only the Python timeout needed by the measured
   87-command gate.

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
Python timeout is therefore 45 minutes. The PR must still supply the first Ubuntu-hosted run.
The optional macOS browser soak could not start Chromium because the host denied its Mach
service registration; use the PR's non-required Ubuntu browser job for the browser result.

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
