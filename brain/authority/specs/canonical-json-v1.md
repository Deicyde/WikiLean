# WikiLean canonical JSON and logical hashes, version 1

Status: normative for every `brain/authority` contract that names
`canonical-json-v1`, including the version 1 and version 2 schemas below.

## Canonical JSON bytes

A canonical JSON v1 value is encoded as UTF-8, with no byte-order mark and no
trailing newline. Objects use lexicographically sorted Unicode scalar-value keys,
arrays preserve their declared order, and no insignificant whitespace is emitted.
Strings and object keys MUST already be Unicode NFC; validators reject rather than
silently normalize non-NFC input. JSON escapes use the shortest JSON spelling except
for the characters that JSON requires to be escaped. Non-ASCII characters are
encoded directly as UTF-8.

The allowed value types are object, array, string, integer, boolean, and null.
Floating-point numbers, exponent notation, `NaN`, and infinities are forbidden.
Integers use base-10 JSON grammar, MUST NOT use `-0`, and MUST be in the exactly
portable range `[-9007199254740991, 9007199254740991]`. Duplicate object keys are
invalid at every nesting depth. An absent member and a member whose value is null
are distinct logical values. Schemas decide which form is permitted; encoders MUST
NOT add, remove, or rewrite null members.

Canonical JSON v1 is equivalent to Python's `json.dumps` with `ensure_ascii=False`,
`sort_keys=True`, `separators=(",", ":")`, and `allow_nan=False`, after enforcing the
additional NFC, duplicate-key, integer-only, and integer-range rules above.

## Domain-separated hashes

All identifiers and logical roots in these schemas use SHA-256 and the lowercase
text form `sha256:<64 hexadecimal digits>`. The preimage is:

```text
utf8("wikilean\0" + domain + "\0canonical-json-v1\0") || canonical_json(value)
```

The domain is an exact ASCII string named by the relevant contract. Domain strings
are not interchangeable.

| Value | Domain | Identity projection |
|---|---|---|
| Source manifest | `wikilean.source-manifest.v1` | Remove top-level `source_manifest_id` and `audit`. |
| Source set root | `wikilean.source-set.v1` | Sorted array of source-manifest IDs. |
| Offline pack | `wikilean.offline-pack.v1` | Remove top-level `offline_pack_id` and `audit`. |
| Source manifest v2 | `wikilean.source-manifest.v2` | Remove top-level `source_manifest_id` and `audit`. |
| Reducer-input inventory v2 | `wikilean.reducer-input-inventory.v2` | Remove top-level `inventory_id`. |
| Source set root v2 | `wikilean.source-set.v2` | Object containing the inventory ID, sorted source-manifest IDs, and input-ID-sorted exact present/absent logical bindings whose members are path-sorted. |
| Offline pack v2 | `wikilean.offline-pack.v2` | Remove top-level `offline_pack_id` and `audit`. |
| Release | `wikilean.release.v1` | Remove top-level `release_id`, `attestations`, and `created_at`. |
| Build attestation | `wikilean.build-attestation.v1` | Remove top-level `attestation_id` and `recorded_at`. |
| Full-replay build attestation | `wikilean.build-attestation.v2` | Remove top-level `attestation_id` and `recorded_at`. |
| Validation attestation | `wikilean.validation-attestation.v1` | Remove top-level `attestation_id` and `recorded_at`. |
| Logical JSON artifact | `wikilean.logical-json.v1` | Remove a top-level `_meta` member, if present. |
| Logical JSONL rowset | `wikilean.logical-jsonl-rowset.v1` | See below. |

The release projection excludes attestation references to avoid a content-identity
cycle: attestations bind the already computed release ID. A validation attestation
must not embed the final manifest digest, because the manifest embeds the attestation
digest; exact manifest bytes are instead content-addressed by the registry or caller
that stores the manifest. Changing an attestation reference therefore requires a new
immutable manifest object but does not rename the release's logical content.

## Logical artifact and rowset roots

Authority manifests use the integer-only canonical JSON rules above. Generated JSON
artifacts additionally allow finite JSON decimal numbers because current layout and
Artifact decimals are parsed exactly (never through
binary floating point), rendered in plain base-10 notation with no exponent, trailing
fractional zeroes, or negative zero, and hashed with the domain preimage suffix
`canonical-artifact-json-v1`. `NaN` and infinities remain forbidden. Artifact strings
preserve their exact Unicode code points, including legacy non-NFC source text; the
NFC requirement remains mandatory for authority manifests and schemas.

A JSONL logical rowset is parsed one non-empty line at a time. A line containing only
`{"_meta": ...}` is audit/representation metadata and is omitted. Remaining rows are
artifact-canonicalized individually, sorted by their canonical byte sequence, and hashed
as one canonical JSON array under `wikilean.logical-jsonl-rowset.v1`. Duplicate rows
remain duplicated and therefore change the root.

A JSON logical artifact is parsed strictly. If it is an object with a top-level
`_meta`, that member is omitted. The remaining artifact-canonical value is hashed under
`wikilean.logical-json.v1`. Artifacts declared `opaque` have `logical_root: null` and
are protected only by their exact byte digest and length.

Logical roots describe representation-independent logical content. They never hash
database iteration order, file mtimes, local paths, physical SQLite bytes, audit-only
timestamps, or a hash field that contains the resulting value. Exact artifact
SHA-256 values remain mandatory and protect the published bytes separately.
