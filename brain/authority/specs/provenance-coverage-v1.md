# Standalone provenance coverage v1

This check covers emitted provenance labels and their exact input/policy mappings.
It does not reconstruct every graph operation, determine source rights, authenticate
reviewers, accept graph changes, approve a baseline, or authorize production use.
Existing reducer output, registry bytes, inventories, pack identities, policy
restrictions and release gates remain unchanged.

## Trust and implementation boundary

The operator independently supplies both the mapping ID and approved private policy
review ID. An optional public policy review also needs its independently selected
ID. Choosing those IDs is a trusted local review decision. The checker does not
infer approval from a document's self-reported ID, source label, or license name.

The mapping binds the exact verified v3 pack, source-set root, inventory, reducer
file/configuration descriptor digest, release and release artifact root. Its complete
input binding snapshot records every logical member, source manifest ID, normalized
object name, native source pin, content digest, size, media type and roles. Exact
optional absence is included. Its registry reference must be the sole bound
`source-registry` object and match the released registry bytes.

`IMPLEMENTATION_PATHS` is an immutable five-path tuple. `implementation()` returns
immutable `(path, sha256, bytes)` triples captured at module import, checks local
module origins, and refuses changed disk bytes. The whole local closure is the
checker, family dispatcher, standalone policy contracts, authority contracts and
execution environment contracts. `implementation_root()` hashes the sorted record
list under `provenance-coverage-implementation/v1`. Both mapping and report bind this
root. The caller is responsible for an isolated, measured Python/standard library
runtime. This library does not attest its own runtime or acquire sources.

## Mapping control

The exact top-level keys are `schema`, `mapping_id`, `scope`,
`implementation_root`, `binding`, `state`, `reviewer`, `input_bindings`, `registry`,
and `rules`. Schema is `wikilean.provenance-coverage-mapping/v1`.
`mapping_id` is `domain_hash(schema, document without mapping_id)` using the existing
canonical-json-v1 contract. `state` is `pending` or `reviewed`; a pending draft has
no reviewer. A reviewed mapping records a bounded name and UTC `reviewed_at`.
Neither state authenticates the reviewer or licenses content.

Every rule has exactly `id`, `family`, `match`, `primary_inputs`,
`supporting_inputs`, and `policy`. IDs are unique and sorted. `match` is an exact
source/method pair, plus the exact `queue` field for `tag-queue`; no regex, alias,
free-form predicate or pin is accepted. The fixed 25-family dispatcher verifies
primary/support group selection. Actual claim context additionally constrains edge
kind and endpoint types/database. Multiple rules for one exact match are rejected.

Policy has exactly `basis`, `registry_entries`, `supplement`,
`source_manifest_ids`, and `evidence_ids`. Registry entries are absolute JSON
pointers to exact entry objects. A supplement has exactly `source` and
`description`; `tag-queue` and `wikilean` require explicit supplements because the
pinned registry lacks those literal labels. Every rule includes all selected
primary/support source IDs and their complete normalization ancestry, including
sources proving optional absence. Every source needs an approved private review
entry, and each cited evidence ID must exist in that pinned review. Entry, basis,
and evidence omissions remain failures. Meaningful legal basis and semantic
appropriateness of the selected entries remain the operator's review.

## Enumeration and input witnesses

The checker first runs existing full pack, SQLite projection, static closure,
release and policy verification. It then streams every nonopaque release artifact,
using the artifact JSON grammar (exact decimals and original Unicode). It enumerates
all direct `provenance` dictionaries, every declared pool entry including unused
entries, and every integer `prov` reference. The supported pools are cells/synapses
first-row metadata and the cell manifest's main/trace pools. Arbitrary nested pools,
noninteger/bool/negative/out-of-range references, malformed direct values and base
edges without provenance fail. The original pool selection is preserved for static
cards and traces. Opaque SQLite is covered by the existing full projection verifier;
no second, partial SQLite query is substituted for it.

Each occurrence resolves its exact primary/support input members. External objects
are selected by their database filename, user repositories by printed source pin,
and D1 article organs by the exact article slug. The historical printed pin must
match the designated first primary input. Additional inputs of mixed-source joins
are still required: for example the Erdos/OEIS problems.yaml join binds both Formal
Conjectures and the Erdos join source. A single label is never assumed to be a source
manifest name or a complete statement of all inputs.

Family rules identify the direct producer inputs and named secondary joins. General
base-stage node/topology materialization is bound by the complete pack input snapshot,
reducer and private policy closure; it is not repeated as a semantic dependency proof
on every family rule. In particular, queue witnesses check candidates/status while
full rejection/override/merge reconstruction remains a separate reducer check.

Simple witnesses additionally check original tag rows, Wikidata relation/property
rows, external page/link rows, OpenAlex citation rows, accepted containers, verified
discoveries, FC fold rows, queue candidates/status and annotated D1 article members.
Rows are indexed by exact artifact keys; member-selection roots avoid serializing
large source trees for every edge. Queue or collapsed-organ witnesses can be
partial because that projection may omit original endpoints. Other methods are
explicitly `not-reconstructed`; a bound join is not claimed to prove the entire
semantic transformation. Witness status is separately counted. The report never
waives pending graph differences or licenses descriptive/code fields merely because
their provenance label has a mapping.

## Report and use

The exact report schema is
`brain/authority/schemas/provenance/coverage-report-v1.json`, with additional identity,
count-sum, readiness, limits, uniqueness and truncation checks in `validate_report`.
`report_id` is `domain_hash(schema, document without report_id)`. The sole readiness
field is `provenance_coverage_ready`; it is true only with zero failures and every
enumerated occurrence covered. The report binds the mapping, private/optional public
reviews, implementation and release/pack descriptors. Occurrence and resolved-input
roots hash ordered canonical artifact/control records, one newline after each.
It retains at most 200 diagnostic details while counting every failure. Limits are
8 MiB per JSONL line, 128 MiB per parsed JSON/witness object and 20 million occurrences.

The isolated CLI accepts `draft --pack ... --release ...` and emits only a pending
canonical mapping. `check` additionally requires mapping/private-review documents
and independent expected IDs, with attachment roots as appropriate. Output goes to
stdout; unsuccessful coverage returns exit 2. It never writes an approved mapping.

A finalization gate must call `check()` again against retained exact inputs and
recheck the implementation tuple before/after and before publication. It must not
accept a caller-authored report as evidence that a check ran. Private replay policy
is a separate prerequisite before execution; release-specific coverage occurs only
after the release exists. Public artifact/field policy, semantic reconstruction,
legacy/SQLite parity and authority acceptance remain separate requirements.
