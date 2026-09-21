# Experimental assertion fixtures v1

Status: frozen experimental P2A contract. This protocol is for offline shadow
validation only. It is not accepted `changeset/v1`, a genesis ceremony, an
authenticated writer, a source transition, or a production authority path.
Outputs explicitly carry `authority: false`. Future accepted authority must use
its own reviewed envelope and policy; do not reinterpret existing v1 fixtures.

The executable validator is `brain/tools/assertion_kernel.py`. The retained
`fixtures/assertion-history-v1.json` contains fixed operation, state and chain
identities for creation, retraction and restoration. Runtime inputs, release
`authority.through_changeset`, the Worker and D1 are unaffected.

## Encoding and identity

Use [canonical-json-v1](canonical-json-v1.md): UTF-8, NFC strings, lexicographically
sorted object keys, exact portable integers, booleans, null, arrays and objects.
Duplicate JSON keys, non-NFC strings, floats and unsupported types fail. File
whitespace and object serialization order do not affect identity. Unknown fields
or schema versions fail. An absent field differs from null; every envelope field
below is required. Audit timestamps and physical paths are absent from this protocol.

All logical IDs are `sha256:` followed by 64 lowercase hexadecimal characters.
Hashing uses the existing `domain_hash(domain, canonical_value)` primitive, with
the `wikilean\0<domain>\0canonical-json-v1\0` prefix.

| Object | Domain | Preimage |
|---|---|---|
| Operation | `wikilean.experimental-assertion-operation.v1` | Entire operation excluding `operation_id` |
| Fixture | `wikilean.experimental-assertion-fixture.v1` | Entire fixture excluding `fixture_id` |
| State | `wikilean.experimental-assertion-state.v1` | Entire canonical state document |
| Equivalence | `wikilean.experimental-assertion-equivalence.v1` | `{operation_type, claim}` as defined below |
| Chain | `wikilean.experimental-assertion-chain.v1` | `{previous_chain_root, fixture_id, state_root}` |
| Empty chain | Same chain domain | `{genesis: true, policy: "wikilean.experimental-assertion-kinds/v1"}` |

An assertion ID is a separately allocated stable identifier. It is never inferred
from its semantic equivalence key. Allocation must expect revision zero (absence),
and an inactive assertion still occupies its ID. Every operation ID has one history
position. Retries or duplicate IDs fail rather than adding another position.

## Operation and fixture envelopes

An operation has exactly `schema`, `operation_id`, `operation_type`, `assertion_id`,
`expected_revision`, `actor`, and `payload`. Schema is
`wikilean.experimental-assertion-operation/v1`.

| Operation type | Exact payload fields | Expected revision |
|---|---|---|
| `assert_entity` | `entity`, `attributes` | `0` |
| `assert_relationship` | `src`, `dst`, `kind`, `attributes` | `0` |
| `retract_assertion` | `reason` | Current positive revision |
| `restore_assertion` | `retraction_id` | Current positive revision |

Attributes are a canonical JSON object. A retraction reason is a nonblank string
of at most 8,192 Unicode code points. Revisions are exact integers, never booleans.
The maximum revision and ledger operation count are 100,000. One operation is at
most 1 MiB and one ledger document at most 64 MiB in canonical encoding.

The fixture actor is exactly `{kind: "fixture", id: <slug>}` with a lowercase
letter followed by at most 63 lowercase letters, digits, underscores or hyphens.
A source actor is exactly `{kind: "git-snapshot", commit, path, row_sha256}`,
where commit is a full lowercase 40-character Git ID, path is a literal safe
repository-relative path, and row SHA-256 is 64 lowercase hexadecimal characters.
These actor fields are attribution claims inside arbitrary fixtures. Only the
separate Git shadow importer actually verifies the named source; replay does not
authenticate people, permissions, upstream ownership, or arbitrary Git claims.

A fixture has exactly `schema`, `fixture_id`, `policy`, `previous_state_root`,
`previous_chain_root`, and nonempty ordered `operations`. Schema is
`wikilean.experimental-assertion-fixture/v1`; policy is
`wikilean.experimental-assertion-kinds/v1`. Both predecessor roots must match.
A ledger has exactly `schema: "wikilean.experimental-assertion-ledger/v1"` and
ordered `fixtures`. An empty fixture sequence represents empty replay.

## Experimental entity and relationship policy

Endpoints are canonical `Q[1-9][0-9]{0,11}` identifiers or a nonempty identifier
with prefix `path:`, `decl:`, `ext:`, or `lit:`. They have at most 1,024 code points
and contain no ASCII control characters below U+0020. Generated `cell:` IDs fail.
This deliberately limited grammar does not establish entity existence, declaration
validity, or the complete future organ policy.

Authored relationship kinds are exactly `formalizes`, `mentions`, `xref`, and
`relates`. Generated `depends`, bulk `links`, and `contains` do not belong to this
fixture protocol. Semantic equivalence uses `{entity}` for an entity assertion or
`{src, dst, kind}` for a relationship. Attributes, actor and provenance are excluded
from equivalence, but remain fully bound by operation identity and state root.
Independent equivalent assertions therefore remain independently retractable.

## State and transitions

State has `schema: "wikilean.experimental-assertion-state/v1"`, `policy`, and
arrays `assertions` and `operations`, each sorted by its unique ID. Every operation
is retained. Each assertion retains creation ID/type/payload/actor, semantic key,
revision, active flag, current retraction ID, ordered operation history, and every
retraction with its exact restoration ID or null. Creation starts at revision 1.

Only an active assertion may be retracted. Only an inactive assertion may be
restored, and restoration must name its exact current, unrestored retraction.
Both transitions increment revision. No operation deletes history or reallocates
an existing assertion ID. The whole fixture applies atomically: a failing later
operation leaves the caller's prior state unchanged.

The semantic state root binds inactive assertions and all history. Restoring the
same active graph therefore does not recover the earlier state root. Chain roots
also bind fixture order. Two independently permuted histories can have equal
semantic roots and different chain roots.

The validator derives each conflict footprint: reads are the kind-policy ID and
the exact assertion revision, plus the exact retraction for restoration; writes
are the assertion ID and operation ID. Callers cannot supply a footprint. The
only registered commutation rule permits operations on distinct assertion IDs
with distinct operation IDs. All four operation classes are tested in all
permutations on disjoint histories. Operations on one assertion do not commute.

Full replay starts empty. Incremental replay first reconstructs the retained
prefix fixtures and compares the entire reconstructed result with the supplied
checkpoint before applying the suffix. A state cannot authorize its own hashes.
This is correctness-first replay, without accepted checkpoint acceleration.

## Legacy contribution shadow

`shadow_assertions.py` reads precisely `brain/data/container_links.jsonl` and
`brain/data/discovery_proposals.jsonl` from an explicitly named native Git commit.
The bounded Git reader checks the commit/tree; dirty or staged worktree content
does not become input. No runtime file or accepted authority path is written.

The assertion ID uses domain `wikilean.experimental-legacy-assertion.v1` over
`{commit, path, row_sha256, equivalent_occurrence}`. The row digest hashes canonical
row bytes; the occurrence starts at 1 separately for identical rows in each file.
This preserves duplicate contributions while making file/row enumeration and JSON
key order irrelevant. This ID scheme identifies a contribution in one exact Git
snapshot; migration to durable origin IDs across snapshots belongs to P2C.

Container rows project to their QID and normalized `path:` endpoint with kind
`formalizes`; discovery rows preserve their actual `src`, `dst`, and `kind`.
Every original row remains in attributes without dropping evidence or review
fields. The shadow comparison projects those rows back and compares canonical
multisets, separately for both source files. It proves contribution parity only;
it does not run the graph's filtering, declaration resolution, cell reducers or
source transitions. Full graph parity remains a P0-R/P2B requirement.
