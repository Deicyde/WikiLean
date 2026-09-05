# Brain authority contracts

This directory contains versioned contracts for sealing reducer inputs and describing
immutable releases. It does not change the current reducer, publication path, D1
overlay, or serving topology.

## Contracts

- `specs/canonical-json-v1.md` is normative for canonical bytes, domain-separated
  identifiers, and representation-independent JSON/JSONL logical roots.
- `schemas/source-manifest/v1.json` describes one acquired source, its native pin,
  raw and normalized objects, licensing, and exact tool identities.
- `schemas/offline-pack/v1.json` describes the closed local set of source manifests,
  source objects, reducer code, configuration, and schemas needed for offline replay.
  Its verification root must contain exactly those files plus the pack manifest.
- `schemas/reducer-input-inventory/v2.json`, `schemas/source-manifest/v2.json`, and
  `schemas/offline-pack/v2.json` are the P0-R contract foundation. The inventory names
  logical repository/external roots, exact required or optional inputs, the complete
  reducer code scope, and a topologically ordered stage DAG whose sorted output lists
  assign every file or tree to one disjoint owner. A v2 source object may
  carry both `raw` and `normalized` roles when normalization is byte-identical; every
  object lives at `objects/sha256/<digest>`. A v2 pack binds every inventory input to
  an exact sorted member list or to explicit absence. Every binding also carries a
  sorted nonempty source-manifest ID set: present bindings name exactly their member
  manifests, while absent bindings retain the manifests that attest the absence. Its
  source-set root covers the inventory ID, all source-manifest IDs, and those bindings.
  The pack separately
  closes all source objects, reducer files, configuration, environment descriptor,
  and schemas. Reducer code is additionally bound to a full Git commit. The reducer
  configuration is one canonical JSON document rather than an ambiguous file list.
- `schemas/offline-pack-source-plan/v1.json` is the compiler input contract. It binds
  every source/configuration/environment/schema object to an expected SHA-256 and byte
  length, maps every reducer input member to its own source manifest (including
  mixed-provenance wildcard inputs) or records explicit absence, carries review/audit/
  normalization metadata into source-manifest/v2, and keeps relocation-dependent host roots
  outside the canonical document.
- `schemas/acquisition-receipt/v1.json` and
  `schemas/normalization-lineage/v1.json` are standalone, self-identifying acquisition
  evidence contracts. A receipt binds the upstream URI/native pin, acquisition tool,
  complete request-set counts, and raw output hashes. A lineage record binds its exact
  receipt/parent-manifest origins, normalization mode/schema/tool/configuration, and output
  hashes. Their required audit timestamps are excluded from logical evidence identities.
  The request-set root is domain-separated over a bounded, sorted, unique list of canonical
  request descriptors. Acquisition URIs allow only canonical lowercase `https`, `d1`,
  `postgresql`, and `git+https`; userinfo, queries, fragments, and control characters are
  rejected. Every request kind, including `http_get`, binds the exact canonical
  query/body/statement/export-parameter bytes through `parameters_sha256`; a
  parameter-free request hashes the empty byte string. This keeps credentials out of the
  receipt URI. The parameter preimage itself must be sealed by the future pack-integration
  contract before the receipt can authorize replay. The executable Python validator is normative;
  the portable JSON Schemas describe shape but cannot express root recomputation, count
  equality, exact origin sets, or identity-mode equality.
  These documents are not silently grafted onto source-manifest/v2 or offline-pack/v2;
  fail-closed pack integration requires an explicit next contract version.
- `schemas/execution-environment/v1.json` gives the environment descriptor its own
  strict, self-identifying contract. It pins the exact CPython, NumPy lock and installed
  tree, SQLite library and compile options, locale, replay-runner closure, and sandbox
  policy. `development-host` records are explicitly diagnostic; only a digest-pinned
  Linux `authoritative-oci` profile can become release-grade evidence. The environment's
  runner Git commit must equal the pack reducer commit. Other runtime byte/root recipes
  become authoritative only when the runner probes them and the OCI profile is frozen;
  descriptor validation alone is not execution evidence.
- `schemas/release/v1.json` describes one release over the current artifact topology.
- `schemas/release-selector/v1.json` describes the public current/previous selector.
  Current release fields are required; the flat `previous_*` fields are all-or-none, and
  each release hex and immutable manifest URL must agree with its full `sha256:` ID.
  Optional `audited_at` records publication audit time without changing release identity.
- `schemas/attestation/build-v1.json` and
  `schemas/attestation/validation-v1.json` describe immutable build and validation
  evidence. Attestations bind a timestamp-independent release ID; they do not embed
  the final manifest digest, which would create a manifest/attestation hash cycle.
- `schemas/attestation/build-v2.json` is reserved for a real
  `full-offline-replay`. It requires the exact offline-pack ID, source-set root,
  reducer-inventory ID, and `network: "disabled"`; the compatibility release freezer
  does not emit it.
- `schemas/build-context/v1.json` documents the strict runtime-only context assembled
  after pack verification: disjoint absolute code/input/output/scratch roots, exact
  materialized bindings and native source pins, the inventory stage schedule, replay
  roots, and reducer configuration. Physical roots, materialized paths, and audit data
  are excluded from its `generation_id`.
- `schemas/reducer-config/v1.json` closes the current semantic reducer knobs: external
  node cap, allowed cell attachment kinds, and layout enablement/iteration count.
- `reducer-inputs-v1.json` classifies current reducer inputs as curated Git inputs,
  immutable source objects, or forbidden ambient state.
- `reducer-inputs-v2.json` is the strict post-acquisition-fold logical inventory. Its
  external roots are names in the contract, never host paths or environment values.

The JSON Schema files are portable documentation. The standard-library Python
validator is the executable authority and intentionally rejects unknown members and
unknown schema versions without requiring the third-party `jsonschema` package.

## Verification

All commands are offline. Paths are relative to `--root` (the manifest directory by
default); absolute paths, `.`/`..` segments, backslashes, missing files, and symlinked
path components are rejected. `uri` is descriptive and is never fetched.

```bash
cd /Users/jackmccarthy/projects/WikiLean
python3 brain/tools/verify_source_set.py --manifest /path/to/source-manifest.json --root /path/to/object-root
python3 brain/tools/verify_source_set.py --manifest /path/to/offline-pack.json --root /path/to/pack-root
python3 brain/tools/verify_source_set.py --manifest brain/authority/reducer-inputs-v2.json --root brain/authority
python3 brain/tools/compile_offline_pack_v2.py \
  --plan /path/to/canonical-source-plan.json \
  --inventory brain/authority/reducer-inputs-v2.json \
  --output-store /path/to/private-offline-pack-store \
  --root repo=/path/to/WikiLean --root external=/path/to/acquired-inputs
python3 brain/tools/preflight_offline_pack_v2.py \
  --plan /path/to/canonical-source-plan.json \
  --inventory brain/authority/reducer-inputs-v2.json \
  --output-store /path/to/private-offline-pack-store \
  --root repo=/path/to/WikiLean --root external=/path/to/acquired-inputs
python3 brain/tools/prepare_replay_v2.py \
  --manifest /path/to/offline-pack-v2.json --root /path/to/pack-root \
  --workspace /path/to/new-workspace --authority-git-commit <40hex> \
  --authority-root sha256:<64hex> --semantic-epoch <epoch>
python3 brain/tools/run_offline.py --manifest /path/to/offline-pack.json --root /path/to/pack-root -- <reducer arguments>
python3 -I brain/tools/run_offline.py \
  --manifest /path/to/offline-pack-v2.json --root /path/to/pack-root \
  --workspace /path/to/new-workspace --authority-git-commit <40hex> \
  --authority-root sha256:<64hex> --semantic-epoch <epoch>
python3 brain/tools/verify_release.py --manifest /path/to/release.json --root /path/to/release-root
```

`prepare_replay_v2.py` is prepare-only. It reopens and verifies every copied byte,
copies rather than links the exact normalized input and reducer closures into a private
sibling staging directory, emits the canonical build context with final workspace paths,
makes code/input read-only, synchronizes the tree, and publishes with an atomic
no-replace rename. Caller environment and caller-supplied source paths cannot select
runtime inputs. The tool prints one canonical JSON result and never executes a reducer.
These permission modes are an integrity convention for preparation, not a sandbox; the
executor must still enforce an operating-system boundary while running reducer code.

`compile_offline_pack_v2.py` is also network-free. It validates a canonical source plan,
reads curated inputs and reducer code from the plan's pinned Git commits rather than the
mutable worktree, streams approved source bytes into a deduplicated content-addressed
object closure, rechecks mutable selector membership immediately before sealing, verifies
the generated v2 pack, and publishes it with an atomic no-replace rename. The output store
must be current-user-owned mode `0700`; completed packs are read-only and same-ID reuse is
accepted only after full closure and byte verification. The compiler deliberately does not
prove that upstream acquisition receipts, dataset revisions, or cross-source revisions are
coherent: approving the source plan is a separate fail-closed gate.

`preflight_offline_pack_v2.py` performs the bounded check before a large compile. It reads
canonical metadata, filesystem sizes, directory membership, and pinned Git object sizes but
does not hash or copy corpus payloads. Its canonical report distinguishes structural errors
from release-readiness concerns and summarizes required/optional presence, pin strength,
freshness, receipts/lineage, redistribution policy, and conservative free-space headroom.
The report is explicitly scoped to `source-plan-only` and records that the runtime
environment was not checked. `compile_ready` covers structural compiler admission;
`source_authority_ready` and `source_publishable` cover only source-plan evidence and policy,
not replay, runtime, release, or deployment readiness. The CLI exits 2 whenever
`source_publishable` is false, including warning-class concerns that block source authority
or publication. Compilation still performs the authoritative corpus-byte hashing and
complete pack verification.

`run_offline.py` supports both contract generations. For v1 it remains the cooperative
single-program fixture runner: it verifies complete pack closure, then executes one Python
reducer with fixed locale/timezone/hash settings, an allowlisted environment, and a
fail-fast socket monkeypatch. This catches accidental Python network calls but is not a
security sandbox.

For v2, `run_offline.py` requires a fresh workspace and explicit authority identity,
prepares the sealed context, and delegates to `run_replay_v2.py`. That executor verifies
the exact input and reducer closures before execution, rechecks reducer bytes after each
stage, executes all seven stages in inventory order (including independent leaves), rejects
caller-supplied stage arguments, and validates stage-owned outputs, scratch cleanup, and
predecessor immutability after every stage. Reducer processes
must run inside the supported Darwin sandbox or Linux bubblewrap boundary; missing or
unsupported isolation fails closed, networking is denied, and host writes are confined to
output/scratch. Linux additionally provides an ephemeral isolated `/tmp`; it exposes the
exact prepared workspace plus selected runtime roots rather than
the host root; Darwin limits reads to those roots plus Apple's standard system runtime
profile and denies process forks. Offline-pack/v2 now requires a canonical
`execution-environment/v1` descriptor. Preparation copies those exact bytes into the
workspace as a private, read-only file, and the runner revalidates its digest, canonical
form, identity, and reducer Git binding before selecting a sandbox or executing a stage.
Before stage one, it now verifies the live interpreter, NumPy import/runtime tree, SQLite
extension/build, effective UTF-8/hash settings, fixed runner-file closure, development-host
fingerprint, and sandbox executable/version/structural policy. A bounded canonical probe is
executed through the same interpreter flags, sanitized environment, and kernel boundary as
the reducer stages. The public CLI rejects `authoritative-oci` descriptors until a trusted
launcher can prove that it started the exact platform manifest digest; a caller-authored
marker or evidence file is not sufficient. Clean-host integration tests and a fixed NumPy/
BLAS CPU-dispatch policy are still required before this counts as authoritative clean-room
evidence. The v2 CLI requires Python
isolated startup; the original invocation must use `python3 -I ...` because Python startup
hooks run before application code can sanitize its own process.

`brain/test_replay_sandbox.py` runs hostile probes through the production boundary: it
checks read/write confinement, symlink escape, loopback networking, temporary storage,
and platform-specific fork/capability behavior. Hosts that cannot create a nested sandbox
report an explicit skip. Clean-host evidence runs set
`WIKILEAN_REQUIRE_REPLAY_SANDBOX=darwin` or `linux`, which converts a missing, unusable,
or mismatched boundary into a hard failure.

Executing fixtures through this path is not by itself a `full-offline-replay` attestation.
No real full-corpus v2 pack, pinned execution environment, two-path deterministic build, or
approved-baseline compatibility result is claimed yet.
The compiler proves curated Git membership and bytes against the declared commit/tree and
exhausts every inventory selector. It does not yet prove non-Git acquisition/revision
coherence, and no v2 build attestation may be emitted until that separate gate passes.

Verification includes canonical encoding, schema/version and unknown-field checks,
self-identities, source-set closure, exact SHA-256 and byte lengths, offline-pack
closure with no unreferenced or undeclared files, artifact logical roots, build-to-
reducer attestation linkage, exact SQLite payload/index/owner/metadata parity with JSONL,
exact static cell-card, alias, label, and trace projections with no stale files, and current
graph/cell generation consistency.

Opaque source objects, reducer files, and release artifacts are hash/size verified as
bounded-memory streams. Only structured documents that must be parsed or compared are
materialized in memory, so multi-gigabyte corpus members do not create equivalent peak
allocations during pack or semantic-diff verification.

Until accepted changeset replay lands, production releases use the compatibility
semantic epoch `brain-v3-current` with `authority.through_changeset` null. The semantic
state root is domain-separated over that epoch, the shared 64-hex graph `snapshot_id`,
and the verified logical roots of the seven graph, cell, synapse, and frontier outputs.
The compatibility `source_set_root` is separately domain-separated over the canonical
`reducer-inputs-v1.json` digest and every exact present/absent path or glob member in
that inventory. This is a declared-input bridge, not a claim that Phase 2 accepted
source-manifest transitions already exist.

The `brain-current-v1` release profile requires both current graph edge streams
(`edges.jsonl` and `edges_links.jsonl`), nodes, SQLite, cells, synapses, frontier files,
the sealed source registry and community-edge
input, complete static cell tree, top-level source and xref indexes, and generated Brain
page. Review/worklist outputs remain build or
validation diagnostics rather than serving artifacts. This profile deliberately does
not require R2, PostgreSQL, or a database topology change.

`brain/tools/build_release.py` reads only completed mutable outputs, freezes their exact
closure under a temporary sibling of the output store, derives all hashes from the
frozen bytes, creates canonical build and validation attestations after release identity,
verifies the complete candidate, and atomically renames it to
`site/out/brain-releases/<release-hex>/`. Existing content-addressed releases are reused
only after full verification and byte equality. The CLI prints one JSON object containing
`release_id`, `release`, `root`, `manifest`, `artifact_count`, `byte_count`, and `reused`.
Its `build-attestation/v1` records compatibility release assembly, not a clean-room graph
replay. `release/v1` continues to require that v1 attestation until the v2 replay path is
exercised with a real pack, the execution environment and pack-only isolation are pinned,
and the resulting dual build is independently verified.

## Phase 1 activation evidence

The promoter's retained dry-run mode freezes the exact sealed public tree, Worker bundle,
Wrangler configuration, and raw read-only selector/status/history responses into an
external content-addressed root. The proposed intent refers to those durable bytes.

`site/ops/brain_activation_ci.py` emits canonical
`wikilean.brain-activation-ci/v2` evidence for the exact required CI commands. Bundle
`freeze` invokes it in-process, so caller-authored CI evidence is not accepted. It requires
a clean promotion checkout whose `HEAD` and `refs/heads/main` equal the candidate authority.
Git, Node, npm, and Python are explicit absolute paths; caller `PATH` is discarded and a
private shim directory pins child-tool resolution. The recorder checks Node 22 and Python
3.12, removes inherited credentials and Git overrides, bounds and cleans up every process
group, and repeats the Git authority/cleanliness fence afterward.

`site/ops/brain_activation_bundle.py context|freeze|verify` creates the immutable P1B
review artifact. `context` proves the isolated build and clean promotion worktrees are
distinct and share the candidate authority. `freeze` validates and atomically publishes
exactly 11 canonical evidence files to the external
`WIKILEAN_BRAIN_ACTIVATION_BUNDLE_STORE`; `verify` rechecks the frozen bundle without the
original mutable worktrees or release stores, but it requires the referenced retained
promoter-artifact companion root. Freeze records a fresh fixed-setting candidate SQLite
measurement, requires a reviewed non-self semantic baseline ID, proves the retained
non-Brain public closure equals the baseline, and re-verifies the retained promoter
artifacts before and immediately before publication.

The included `wikilean.semantic-diff/v2` report must completely bind the logical roots for
the seven compatibility paths: `nodes.jsonl`, `edges.jsonl`, `edges_links.jsonl`,
`cells.jsonl`, `synapses.jsonl`, `frontier.jsonl`, and `frontier_graph.json`. Complete
release verification separately covers SQLite and release-coupled static artifacts.
Tooling completion is not activation: generating the first P1B bundle remains blocked on
Jack merging P1A to `main` and authorizing the host Mathlib/interpreter paths, and P1C still
requires separate deployment approval.
