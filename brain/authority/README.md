# Brain authority contracts

This directory contains the first versioned contracts for sealing reducer inputs and
describing immutable releases. It does not change the current reducer, publication
path, D1 overlay, or serving topology.

## Contracts

- `specs/canonical-json-v1.md` is normative for canonical bytes, domain-separated
  identifiers, and representation-independent JSON/JSONL logical roots.
- `schemas/source-manifest/v1.json` describes one acquired source, its native pin,
  raw and normalized objects, licensing, and exact tool identities.
- `schemas/offline-pack/v1.json` describes the closed local set of source manifests,
  source objects, reducer code, configuration, and schemas needed for offline replay.
    Its verification root must contain exactly those files plus the pack manifest.
- `schemas/release/v1.json` describes one release over the current artifact topology.
- `schemas/release-selector/v1.json` describes the public current/previous selector.
  Current release fields are required; the flat `previous_*` fields are all-or-none, and
  each release hex and immutable manifest URL must agree with its full `sha256:` ID.
  Optional `audited_at` records publication audit time without changing release identity.
- `schemas/attestation/build-v1.json` and
  `schemas/attestation/validation-v1.json` describe immutable build and validation
  evidence. Attestations bind a timestamp-independent release ID; they do not embed
  the final manifest digest, which would create a manifest/attestation hash cycle.
- `reducer-inputs-v1.json` classifies current reducer inputs as curated Git inputs,
  immutable source objects, or forbidden ambient state.

The JSON Schema files are portable documentation. The standard-library Python
validator is the executable authority for v1 and intentionally rejects unknown
members and unknown schema versions without requiring the third-party `jsonschema`
package.

## Verification

All commands are offline. Paths are relative to `--root` (the manifest directory by
default); absolute paths, `.`/`..` segments, backslashes, missing files, and symlinked
path components are rejected. `uri` is descriptive and is never fetched.

```bash
cd /Users/jackmccarthy/projects/WikiLean
python3 brain/tools/verify_source_set.py --manifest /path/to/source-manifest.json --root /path/to/object-root
python3 brain/tools/verify_source_set.py --manifest /path/to/offline-pack.json --root /path/to/pack-root
python3 brain/tools/run_offline.py --manifest /path/to/offline-pack.json --root /path/to/pack-root -- <reducer arguments>
python3 brain/tools/verify_release.py --manifest /path/to/release.json --root /path/to/release-root
```

`run_offline.py` first verifies complete pack closure, then executes a Python reducer with
fixed locale/timezone/hash settings, an allowlisted environment, and a fail-fast socket
monkeypatch. This catches accidental Python network calls but is not a security sandbox;
authoritative CI/build runners must additionally disable networking at the OS or container
layer.

Verification includes canonical encoding, schema/version and unknown-field checks,
self-identities, source-set closure, exact SHA-256 and byte lengths, offline-pack
closure with no unreferenced or undeclared files, artifact logical roots, build-to-
reducer attestation linkage, exact SQLite payload/index/owner/metadata parity with JSONL,
exact static cell-card, alias, label, and trace projections with no stale files, and current
graph/cell generation consistency.

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
