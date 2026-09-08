# Standalone source and release policy reviews, version 1

Status: standalone review tooling; no authority, preflight, replay launcher, or
production promotion integration. The implementation is
`brain/tools/source_policy_reviews.py`; its command line is
`brain/tools/review_source_policy.py`.

These records separate permission to retain and replay a private evidence pack
from permission to distribute particular release bytes. They do not alter any
source manifest, source ID, original redistribution restriction, or ancestor.
They do not authorize a production change, accept an authority changeset, create
genesis, authenticate an author, or determine a legal right.

## Trust and identity

Both documents use canonical-json-v1 and a domain-separated `review_id` over every
field except that ID. The domain is the complete schema string. The operator must
provide the expected review ID separately to validation. Choosing that expected
ID is the trust decision: a reviewer name, a file supplied alongside a pack, or a
self-consistent hash is not authentication. These are unsigned, trusted-local
review records. A party able to choose the expected ID can approve its own record;
the validator deliberately makes no contrary claim.

Every document has an exact `scope`, `state` (`pending`, `approved`, `rejected`),
reviewer audit metadata, sealed evidence references, and complete scoped
decisions. Draft commands emit `pending` only and cannot mint approval. Completed
records require a named `trusted-local-operator` and UTC review timestamp; these
fields are audit metadata. Every approved decision requires a basis, at least one
retained evidence reference, and no unresolved condition. Required obligations
must identify evidence or exact public artifacts that fulfill them. The semantic
adequacy of the basis and fulfillment is the operator's review.

Evidence is either an exact named object of the verified pack or a separately
retained review attachment. Attachments bind a relative path, SHA-256, byte count,
media type, and origin description; the latter is a reviewed assertion, not a new
upstream acquisition receipt. Evidence IDs cover all these fields. Referenced
evidence must exist, match its bytes, and be used by a scoped decision. Separate
attachments allow a subsequently retained license notice to support policy
without rewriting a historical acquisition or changing the source's identity.

## Private replay review

`wikilean.private-replay-policy-review/v1` binds:

- The exact verified v3 offline pack ID, source-set root, and reducer inventory.
- Every source manifest exactly once, including source kind, native pin, original
  license, and its complete object-role closure root/count.
- The global closure of source ID, object name, content digest, byte count, media
  type, roles, and original redistribution restriction. Equal content in distinct
  named source objects remains distinct in this closure.
- Each source's private acquisition/retention/replay decision and obligations.

The pack's original files and evidence are fully verified before a readiness
result. A review of some sources cannot approve an omitted evidence-only ancestor.
An approved private review may retain sources marked `restricted`; their labels
remain unchanged. Its sole readiness field is `private_replay_policy_ready`.
Existing `source_authority_ready` and `source_publishable` results are unaffected.
Compilation already permits a private pack, so the review can bind a compiled
pack without a circular source or pack ID.

## Public release review

`wikilean.public-release-policy-review/v1` additionally binds an independently
expected approved private review ID and a fully verified
`brain-offline-replay-v1` release from that exact pack. It includes the release ID,
whole release-manifest SHA-256, and complete artifact-manifest root. The whole
manifest hash also binds its attestation references.

Each source is explicitly classified `evidence-only` or `content-parent`; every
source has a decision, even when none of its content is distributed. Every release
artifact has an exact byte identity, decision, obligations, and reviewed field
rules naming sources, content classes, license expressions, and interpretations.
Only program-generated material may omit a source ID. A content-parent must occur
in a field rule; an evidence-only source cannot occur in one.

The validator also identifies every artifact whose complete bytes equal a named
source object. All such aliases must be listed and covered by content rules. The
remaining source-object closure root records which original object bytes are not
directly exported. This check is an exact-byte overlap check, **not** proof that a
transformation contains no protected content. Classification of derived fields,
the completeness of field rules, copyright scope, attribution sufficiency, and
whether a particular output is a fact remain explicit trusted-local decisions
over the exact approved artifact bytes. The tool does not execute field selectors
or infer a license grant from an SPDX string.

`release.json` and both attestation files require separate byte-bound public
metadata decisions. Public coverage therefore includes all delivered files, not
only SQLite or the user-facing graph. Notices may reference exact release paths;
unknown notice paths fail validation. Other private evidence, including acquired
HTML, full mixed-license CSVs, raw snapshots, and help-page responses, does not
become public merely because some derived artifact is approved.

The only public readiness field is `public_release_policy_ready`. Version 1
rejects legacy packs/releases and leaves their existing behavior intact. No
production gate consumes these results yet. Any later integration must have an
explicit reviewed profile/version boundary and preserve legacy behavior.

## Command line

All commands print canonical JSON without modifying their inputs or an output
store. Paths are absolute; controls and attachments reject symlinks, special files,
unstable reads, duplicate JSON keys, noncanonical encodings, and excessive sizes.
The review control bound is 128 MiB; individual attachments are bounded at 32 MiB.

```text
review_source_policy.py draft-private --pack /absolute/pack.json
review_source_policy.py validate-private --pack /absolute/pack.json \
  --private-review /absolute/private-review.json --expected-private-id sha256:… \
  --private-attachments /absolute/private-policy-evidence
review_source_policy.py draft-public --pack /absolute/pack.json \
  --release /absolute/release.json --private-review /absolute/private-review.json \
  --expected-private-id sha256:… --private-attachments /absolute/private-policy-evidence
review_source_policy.py validate-public --pack /absolute/pack.json \
  --release /absolute/release.json --private-review /absolute/private-review.json \
  --expected-private-id sha256:… --private-attachments /absolute/private-policy-evidence \
  --public-review /absolute/public-review.json --expected-public-id sha256:… \
  --public-attachments /absolute/public-policy-evidence
```

Draft commands exit 0. Validation exits 0 only for the exact approved scope, 2 for
a valid pending/rejected review, and 1 for an invalid control, mismatched expected
identity, failed evidence check, or incomplete approval. No CLI approves a draft.
