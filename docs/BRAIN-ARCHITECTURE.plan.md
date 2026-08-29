# WikiLean Brain: sustainable authority, storage, and release plan

## Context

WikiLean currently uses several appropriate but incompletely separated representations:

- committed JSONL snapshots for review, recovery, and interchange;
- a generated, gitignored SQLite projection for indexed local queries;
- generated Brain v3 cells, synapses, frontier data, and static Cloudflare shards;
- D1 rows for immediate community edits and tombstones;
- Python reducers that acquire sources, fold proposals, derive graph state, and publish artifacts.

The SQLite migration solved local query performance and generation validation. It did not solve the principal long-term problem: global generated snapshots remain a poor authoring and review format. Recent work demonstrated this directly: a generated diff replaced roughly 280,000 lines while making a much smaller provenance change, and a live rebuild could not reproduce 6,755 Lean snippets because the configured checkout was absent and the recorded source pin did not identify a recoverable checkout.

Repository measurements show that graph size is not the immediate storage constraint:

| Surface | Current | Five-year base case |
|---|---:|---:|
| Organ nodes | 63,359 | about 284,000 |
| Organ edges | 577,060 | about 3.70 million |
| Cells | 21,071 | about 94,000 |
| Synapses | 110,024 | about 705,000 |
| SQLite | 406.4 MiB | about 2.60 GB |
| Static release | about 99.8 MB | about 534 MB |

The measured public workload is selective lookup and bounded 1-3-hop traversal. Indexed SQLite and static shards are suitable for that workload.

A source census also changes the event-ledger design materially: approximately 99% of current edges are deterministic observations derived from source datasets, not individually curated assertions. The durable authority model must therefore distinguish:

1. curated semantic intent;
2. reviewed transitions between immutable source sets;
3. generated observations and delivery projections.

Creating one Git event for every derived edge would replace JSONL churn with an equally unsustainable multi-million-event ledger.

## Goal

Make Brain authorship, review, replay, publication, rollback, and offline use sustainable for at least five years while preserving the current fast static serving model.

Success means:

- pull requests review compact semantic changes instead of generated global snapshots;
- every generated fact traces to reviewed intent or an immutable source object;
- a fresh clone plus an offline source pack can reproduce logical graph state without credentials or network access;
- cells remain derived and do not leak into authored identity;
- D1 retains immediate live editing but cannot silently become a second accepted authority;
- releases are immutable, generation-consistent, and rollbackable;
- PostgreSQL, R2, or a graph database are added only when observed workload justifies them.

## Decision

Adopt a staged Git-native authority and immutable-release architecture.

### Mandatory target

1. **Git is accepted authority.** Protected Git history contains immutable curated changesets, immutable source-set transitions, schemas, reducer schedules, policies, and semantic migrations.
2. **The reducer is deterministic and offline.** It consumes accepted authority plus sealed source objects and produces a versioned logical graph state.
3. **A release is one immutable manifest.** JSONL compatibility exports, SQLite, cells, synapses, frontier data, static shards, and the Brain page are projections named by one release manifest.
4. **D1 is a release-pinned live overlay.** Community edits remain immediately visible under policy, but promotion to accepted state occurs through reviewed Git authority while preserving stable identities and tombstones.

### Threshold-triggered target

5. **PostgreSQL is an optional rebuildable projection.** Design its contract now, but deploy it only when measured collaboration, history, or query requirements exceed SQLite/static capability.
6. **R2 is an optional immutable artifact store.** Keep Worker Static Assets during early phases; introduce R2 only when release size, object count, retention, or rollback requirements justify it.
7. **A graph-native database is an optional read projection.** Reconsider it only if required variable-depth traversal or graph algorithms miss measured targets on indexed relational/static projections.

## Core principles

### Curated authority versus generated observations

Git contains:

- curated assertion, retraction, evidence, override, identity, and moderation decisions;
- reviewed source-set transitions;
- semantic schemas, reducers, migrations, and policies;
- small release manifests and immutable validation attestations.

Git does not contain:

- one operation per deterministic dependency, containment, or external-link edge;
- crawler telemetry or model transcripts;
- large raw source snapshots;
- generated SQLite databases;
- complete generated graph and static-release trees on every build.

A source-set transition authorizes deterministic reduction of an immutable source snapshot. Its expanded edges are generated release state, not individual Git operations.

### Stable identity

- Authored operations target stable organ/entity IDs, never `cell:` IDs.
- Every provenance-bearing assertion has a stable `assertion_id` distinct from its semantic equivalence key.
- Independently sourced equivalent assertions remain independent.
- Retractions target assertion identity, not only `(src, dst, kind)`.
- Every release publishes organ-to-cell mapping and cell split/merge reports.

### Deterministic replay

Replay depends only on:

- the accepted first-parent authority chain;
- immutable source objects named by accepted source manifests;
- an explicit semantic epoch and reducer schedule;
- reducer configuration and implementation identity.

Replay performs no network calls and reads no ambient mutable checkout, D1 state, wall clock, locale, random source, or unlisted cache.

### Generation consistency

Every release-coupled asset URL, API response, cache key, cursor, and overlay response identifies one release. Mixed generations fail closed.

## Authority invariants

1. Accepted authority is a single-parent, first-parent sequence on the protected canonical branch.
2. Each authority commit contains exactly one ordered changeset manifest, or explicitly declares no authority transition.
3. Every changeset names its predecessor semantic root and complete ordered contents.
4. Landing compare-and-swaps against the exact protected head and candidate tree that were validated. Head movement forces revalidation.
5. Accepted authority paths are mechanically append-only. CI rejects modification, deletion, rename, mode change, or symlink substitution of landed changesets, source manifests, migrations, and reducer schedules.
6. An operation ID and changeset ID each have exactly one authority position. Idempotent retry returns the original receipt rather than adding a second ledger entry.
7. Canonical encoding is normatively versioned: Unicode normalization, key ordering, numeric grammar, duplicate-key rejection, absent versus null, and allowed value types are specified.
8. Hashes use domain-separated preimages and exclude their own hash field and audit-only timestamps.
9. The semantic root is computed from sorted canonical logical state, not database order or physical SQLite bytes.
10. Semantic history is divided into append-only epochs bound to exact reducer/schema/config schedules.
11. Historical reinterpretation requires an explicit semantic migration, shadow full replay, root verification, review, and atomic generation switch.
12. Empty replay and incremental replay must converge to the same logical state root.
13. Retraction names an assertion and expected revision; restoration names the exact retraction it reverses; supersession prevents cycles and defines active-state effects.
14. Source objects, reducer artifacts, migrations, and evidence needed by accepted authority are permanent replay roots unless an explicit reviewed compaction protocol replaces them.
15. A release is one immutable manifest with one immutable validation attestation.
16. Rollback switches both base release and a compatible overlay generation.

## Authority data model

### Curated operation envelope

Each curated operation includes:

- `operation_schema_version`;
- permanent `operation_id`;
- typed `operation_type`;
- server- or Git-attested actor identity;
- typed semantic payload;
- source and evidence references;
- confidence and causal references;
- canonical content hash.

The operation does not embed a mutable landing head. Landing expectations and rebase proof belong to the containing changeset wrapper so authored operation bytes remain immutable.

Initial operation families:

- `assert_entity`;
- `assert_relationship`;
- `attach_evidence`;
- `set_curated_attribute`;
- `accept_proposal` and `reject_proposal`;
- `record_verification` and `record_semantic_veto`;
- `retract_assertion`, `restore_assertion`, and `supersede_assertion`;
- `register_alias`, `merge_identity`, and `split_identity`.

### Changeset wrapper

Each changeset includes:

- schema version and permanent changeset ID;
- predecessor semantic state root;
- complete ordered operation list and count;
- zero or more source-set transitions;
- validator-derived read/write/conflict footprint roots;
- optional rebase proof;
- canonical changeset hash.

The authoritative validator derives conflict footprints, including expected absence, uniqueness, aliases, semantic keys, policy versions, and source-set dependencies. Client-declared read sets are advisory only. Automatic rebase is permitted only for explicitly registered operation classes proven to commute.

### Source-set transition

A source transition contains:

- typed source name and pin type;
- native immutable revision where available;
- raw and normalized content digests and byte lengths;
- license and attribution metadata;
- acquisition-tool identity;
- normalization schema and tool identity;
- previous accepted source object;
- review summary and expected semantic effects.

Dates, mtimes, local paths, and short opaque dataset identifiers are audit metadata, not source identity.

### Semantic epochs

A reducer schedule binds a contiguous authority range to exact:

- operation and state schemas;
- semantic reducer version;
- configuration hash;
- source normalization contracts;
- canonical encoding version;
- migration rules.

Representation-only upcasters require fixtures proving semantic preservation. Meaning-changing reinterpretation requires a new epoch and explicit semantic migration.

## Git layout

Create:

```text
/Users/jackmccarthy/projects/WikiLean/brain/authority/
  schemas/
    changeset/v1.json
    operations/v1/*.json
    source-manifest/v1.json
    release/v1.json
  changesets/YYYY/MM/<changeset-id>.json
  migrations/<migration-id>.json
  reducer-schedule/<semantic-epoch>.json
  source-manifests/<sha256>.json
```

Use one immutable file per reviewed changeset rather than appending to one shared JSONL journal.

## Landing protocol

1. Read protected branch head and accepted semantic root.
2. Verify canonical encoding, schemas, IDs, hashes, references, source closure, assertion transitions, and validator-derived conflict footprints.
3. Replay the candidate into isolated state.
4. Produce a semantic diff covering curated assertions, source transitions, generated observation deltas by source/kind, provenance changes, snippets, cell split/merge effects, and affected shards.
5. Run policy, license, acceptance, and reproducibility gates.
6. Immediately before landing, compare-and-swap on the exact validated head and candidate tree.
7. If the head moved, rederive footprints and rerun all dependent validation.
8. Land one authority changeset commit on the first-parent chain.
9. Enforce protected-path immutability on every later pull request.

## Sealed sources and offline packs

Separate acquisition from reduction.

A network-enabled acquisition job:

1. fetches one source;
2. records its native immutable identity where available;
3. records license and attribution;
4. writes immutable raw and normalized objects;
5. records content digest and byte length;
6. emits a typed source manifest;
7. never overwrites a content-addressed object.

The network-disabled reducer consumes an offline pack containing every accepted source object, schema, reducer artifact, configuration, and manifest required for a clean build.

Source advancement review shows:

- old/new source revisions and digests;
- acquisition and license metadata;
- generated observations added/removed by source and kind;
- provenance-only changes;
- snippet additions/removals;
- entity births/deaths;
- curated assertions invalidated or newly satisfiable;
- cell split/merge and frontier effects.

This semantic report replaces generated line diffs as the primary data-review surface.

## Deterministic reducer and derivation DAG

The semantic reducer produces stable entities, provenance-bearing assertions, assertion history/current state, the organ graph, and a logical semantic root.

The derived DAG produces:

- compatibility `nodes.jsonl` and edge streams;
- cells and organ ownership;
- synapses and evidence traces;
- frontier partitions and graph;
- layout coordinates;
- SQLite;
- static cell shards and page;
- optional Parquet and RDF exports.

Every derivation records input logical roots, reducer/version/config/environment identity, output logical root, artifact digests, metrics, and tests.

The migration preserves current Brain v3 semantic rules. Exact formalization remains identity fusion; generalization/special-case attach directionally; invocation/related never merge; pages never bridge; broad concepts remain supercell organs; cells remain derived.

Checkpoints accelerate replay but never become authority. Initially create a checkpoint at the first threshold reached: 30 days, one million new authority entries, 60-second replay p95, or 512 MiB reducer working set. Each checkpoint records exact authority position, epoch, reducer schedule, source root, and logical state root.

## Immutable release architecture

### Preserve current serving first

Worker Static Assets already deploy one Worker version and asset set together. Preserve this topology through source sealing, release-manifest work, and the Git-authority migration. Do not introduce R2 and change authority in the same phase.

### Release manifest

One release manifest names:

- release schema and release ID;
- authority Git commit and semantic root;
- source-set root;
- semantic epoch and reducer schedule;
- reducer commit, configuration, and environment identity;
- every artifact URI, digest, byte length, media type, and logical rowset root;
- immutable build and validation attestations;
- compatible overlay generation IDs;
- audit-only creation time.

The release ID excludes incidental timestamps.

### Generation-qualified reads

All release-coupled URLs, cache keys, responses, and cursors include the release ID. This covers cell manifests/shards, aliases, labels, supercells, explorer, frontier, xref index, traces, Brain APIs, and overlay responses. Isolate-lifetime memoization in `/Users/jackmccarthy/projects/WikiLean/wiki/src/brain.ts` and public cache entries must be release-qualified.

### Initial publication

1. Build under a temporary release namespace.
2. Validate manifest closure, logical roots, and artifact digests.
3. Write immutable build and validation attestations.
4. Build one Worker Static Assets version containing the complete release namespace.
5. Deploy only after all gates pass.
6. Verify canary reads return the expected release and digests.
7. Retain the prior Worker version and manifest.

Rollback success is measured end-to-end until release-qualified APIs and assets consistently return the previous release under CDN and isolate caches. Pointer/deployment API duration alone is not the rollback SLO.

### R2 trigger

Introduce R2 first as a shadow artifact store when any threshold is met:

- compressed release exceeds 500 MB;
- release exceeds 10,000 objects;
- upload/deploy exceeds five minutes p95;
- Worker asset limits impair retention or rollback;
- historical release download becomes a product requirement.

After shadow parity and rollback drills, use R2 for immutable content-addressed artifacts. Use a small D1 release-registry transaction for channel compare-and-swap because D1 already exists and can switch the base release and compatible overlay generation together. Do not assume a mutable object overwrite is a CAS primitive.

### Retention

Initially use manual conservative retention:

- all source/evidence/reducer objects rooted by accepted authority: permanent;
- current and previous production releases: mandatory;
- releases referenced by active overlays, investigations, publications, or holds: mandatory;
- 30 daily, 12 monthly, and annual historical releases: recommended after storage-cost measurement.

Defer automatic reachability garbage collection until manifests, holds, overlay references, dry-run reports, and restore drills are mature.

## D1 live overlay

Preserve immediate live collaboration, but distinguish “visible now” from “accepted in the reproducible base.”

### Append-only overlay records

Separate immutable records for:

- authored operation;
- atomic overlay changeset;
- moderation or verification decision;
- rebase result;
- promotion reservation/attempt;
- promotion receipt;
- retraction or restoration operation.

Current status becomes a rebuildable materialized view rather than mutable lifecycle fields on the authored record.

Every overlay changeset receives an atomically allocated monotonic sequence and a base release ID. Multi-operation submissions commit atomically.

### Stable assertions and tombstones

D1 uses the same stable assertion IDs as Git authority. A retraction targets assertion ID. Promotion preserves the origin assertion ID, so a tombstone suppresses that exact promoted assertion. Independent equivalent assertions remain active.

### Deterministic rebase classification

Classify in strict precedence order:

1. schema/policy invalid -> `quarantined`;
2. identity cannot resolve -> `orphaned`;
3. expected state conflicts -> `conflict`;
4. named successor governs the assertion -> `superseded`;
5. same origin assertion already exists in base -> `absorbed`;
6. independent base assertion has the same semantic key -> `equivalent`;
7. stable organ maps to a different derived cell -> `endpoint_remapped`;
8. otherwise -> `unchanged`.

Each terminal class defines visibility, effective semantic contribution, and promotion eligibility. Only proven-safe classes automatically enter the rebased live view.

### Consistent reads and rollback

Every overlay response includes:

- `base_release_id`;
- `overlay_generation_id`;
- `overlay_sequence`;
- an effective graph token.

Clients reject base/overlay release mismatch. Retain immutable overlay materializations per base release so rollback switches to a compatible prior overlay generation rather than attempting an undefined reverse rebase during an incident.

### Crash-safe promotion

1. Select one complete committed sequence interval or explicit operation set.
2. Reserve a fenced promotion ID and immutable operation/hash set in D1.
3. Derive deterministic Git changeset identity and canonical bytes.
4. Validate and land through the protected Git CAS.
5. Enforce unique origin D1 operation receipts in authority projection.
6. Record an immutable D1 promotion receipt.
7. On retry, adopt an already-landed byte-identical changeset rather than minting a duplicate.
8. Rebase remaining overlay operations.

Define D1 backup checksums, RPO, RTO, recurring restore drills, and reconciliation of restored promotion receipts against Git authority.

## Optional PostgreSQL projection

PostgreSQL is not required for the initial authority/release phases and never becomes an accepted-semantic writer.

Deploy a managed shadow projection only when measurement demonstrates at least one need:

- concurrent curator/reviewer queries require a shared accepted-state database;
- cross-release historical queries become a product feature;
- incremental release construction needs shared transactional state;
- representative bounded queries miss 500 ms p95 after SQLite/static optimization;
- the local projection exceeds practical developer download/build limits;
- operational dashboards require shared current/history joins.

If introduced, suggested schemas are:

- `authority_mirror`: accepted changeset, operation, source-transition receipts and cursor;
- `projection`: current/historical entities, assertions, and evidence;
- `derivation`: generation-pinned cells, owners, synapses, and roots;
- `delivery`: releases and immutable attestation references.

Apply each authority changeset atomically after verifying first-parent continuity, epoch schedule, and immutable bytes. Rebuild into an isolated generation and switch serving only after migration ledger, cursor, epoch, source root, reducer identity, table digests, and semantic root verify. Every PostgreSQL-assisted response selects the exact generation named by the release.

Static/SQLite serving must remain complete with PostgreSQL disabled until an explicit later cutover is approved.

## Other projections

Keep:

- SQLite for complete offline lookup and agent use;
- static JSON shards for public locality serving;
- JSONL compatibility exports during migration;
- Parquet plus DuckDB for bulk analysis and historical comparison;
- RDF as a standards/interoperability export.

Reject as accepted authority:

- committed SQLite;
- generated full-snapshot JSONL history;
- one shared append-only JSONL journal;
- D1-only accepted state;
- direct PostgreSQL semantic writes;
- Parquet/lakehouse mutation authority;
- RDF/SPARQL primary authoring;
- Neo4j/Memgraph primary authority.

## Implementation phases

### Phase 0 — fix current correctness and seal inputs

Critical files:

- `/Users/jackmccarthy/projects/WikiLean/brain/fold_proposals.py`
- `/Users/jackmccarthy/projects/WikiLean/site/ops/brain-nightly.sh`
- `/Users/jackmccarthy/projects/WikiLean/brain/build_common.py`
- `/Users/jackmccarthy/projects/WikiLean/brain/build_snapshot.py`
- `/Users/jackmccarthy/projects/WikiLean/brain/build_cells.py`
- `/Users/jackmccarthy/projects/WikiLean/brain/store.py`
- `/Users/jackmccarthy/projects/WikiLean/catalog/data/source_registry.json`

Deliverables:

1. Repair the unreachable FC-link/repository-link/retraction/override/universe-extension block after `_completed_retract_key()` in `/Users/jackmccarthy/projects/WikiLean/brain/fold_proposals.py` and add regression fixtures.
2. Make proposal-fold failure fatal to release publication in `/Users/jackmccarthy/projects/WikiLean/site/ops/brain-nightly.sh`; never silently build from prior folded outputs.
3. Inventory every reducer input as curated Git input, immutable source object, or forbidden ambient state.
4. Define source-manifest and offline-pack schemas.
5. Seal acquisition for the Mathlib/TheoremGraph and other inputs implicated in the recent reproduction failure.
6. Remove live checkout, network, current-time, and unlisted-cache reads from reduction.
7. Specify canonical encoding and logical roots.
8. Add semantic snapshot diffing by source, kind, assertion, provenance, snippets, cell changes, and frontier effects.
9. Add clean-room build, provenance, and license gates.

Acceptance:

- two independent offline builds from one pack produce equal semantic/logical roots;
- reducer network access fails the build;
- every output traces to accepted authority and immutable source objects;
- fold/source mismatch regressions fail before publication;
- current static production behavior remains unchanged.

Rollback: current builders and snapshots remain intact; no authority or serving cutover occurs.

### Phase 1 — one release contract over current outputs

Critical files:

- `/Users/jackmccarthy/projects/WikiLean/brain/build_snapshot.py`
- `/Users/jackmccarthy/projects/WikiLean/brain/build_cell_shards.py`
- `/Users/jackmccarthy/projects/WikiLean/site/build_brain_page.py`
- `/Users/jackmccarthy/projects/WikiLean/wiki/scripts/build-public.ts`
- `/Users/jackmccarthy/projects/WikiLean/wiki/src/brain.ts`
- `/Users/jackmccarthy/projects/WikiLean/wiki/src/brain-api.ts`
- `/Users/jackmccarthy/projects/WikiLean/site/ops/brain-nightly.sh`

Deliverables:

1. Define release-manifest and immutable-attestation schemas.
2. Build graph, SQLite, cells, frontier, shards, traces, xref data, and page under one release ID.
3. Add release-qualified paths, cache keys, response metadata, and cursors.
4. Validate manifest closure and roots before one Worker Static Assets deployment.
5. Add canary verification and documented Worker-version rollback.
6. Retain mutable paths only as temporary compatibility aliases.

Acceptance:

- mixed/corrupt artifacts fail closed;
- all Brain APIs/assets identify one release;
- canary and rollback drills pass with versions in flight;
- rollback convergence is measured under CDN/isolate caches;
- no R2 dependency.

### Phase 2 — Git authority for curated changes and source transitions

Create:

- `/Users/jackmccarthy/projects/WikiLean/brain/authority/`
- `/Users/jackmccarthy/projects/WikiLean/brain/tools/validate_authority.py`
- `/Users/jackmccarthy/projects/WikiLean/brain/tools/replay_authority.py`
- `/Users/jackmccarthy/projects/WikiLean/brain/tools/semantic_diff.py`

Migrate curated intent from:

- `/Users/jackmccarthy/projects/WikiLean/brain/proposals/`
- `/Users/jackmccarthy/projects/WikiLean/brain/data/container_links.jsonl`
- `/Users/jackmccarthy/projects/WikiLean/brain/data/discovery_proposals.jsonl`
- `/Users/jackmccarthy/projects/WikiLean/brain/data/ext_anchor_links.jsonl`
- `/Users/jackmccarthy/projects/WikiLean/brain/data/fc_links.jsonl`
- `/Users/jackmccarthy/projects/WikiLean/catalog/data/grounding_overrides.jsonl`
- `/Users/jackmccarthy/projects/WikiLean/catalog/data/universe_extension.jsonl`
- `/Users/jackmccarthy/projects/WikiLean/brain/data/community_edges.jsonl`

Deliverables:

1. Implement changeset, operation, source-transition, migration, and reducer-schedule schemas.
2. Implement assertion state machines, identity changes, and validator-derived conflicts.
3. Enforce first-parent history, one authority changeset per commit, immutable paths, and landing-head CAS.
4. Import current curated facts/decisions into a reviewed genesis changeset without expanding bulk source observations into operations.
5. Replay authority plus current source set and compare with current accepted releases.
6. Make semantic diff the pull-request review surface.

Acceptance:

- genesis replay is semantically equivalent under documented compatibility rules;
- stale/conflicting changes fail closed;
- only proven commutative changes auto-rebase;
- accepted paths cannot be mutated;
- full and incremental replay converge.

Cutover:

1. quiesce legacy curated writers;
2. seal an inclusive legacy watermark and digest;
3. land/verify genesis root;
4. atomically enable new authority validation;
5. retain old files as compatibility exports.

### Phase 3 — release-pinned D1 overlay

Critical files:

- `/Users/jackmccarthy/projects/WikiLean/wiki/migrations/0010_brain_edges.sql`
- `/Users/jackmccarthy/projects/WikiLean/wiki/src/db/schema.ts`
- `/Users/jackmccarthy/projects/WikiLean/wiki/src/brain-edits.ts`
- `/Users/jackmccarthy/projects/WikiLean/brain/harvest_community_edges.py`
- `/Users/jackmccarthy/projects/WikiLean/.github/workflows/backup-d1.yml`

Deliverables:

1. Add append-only overlay changesets, operations, decisions, rebases, promotion attempts, and receipts.
2. Add monotonic sequences, atomic multi-operation submissions, base release, stable assertions, and expected revisions.
3. Preserve existing APIs through a materialized current view.
4. Implement deterministic rebase and immutable per-base overlay generations.
5. Replace snapshot harvest with fenced exactly-once Git promotion.
6. Define and drill D1 recovery/reconciliation.

Acceptance:

- no tombstone resurrection across promotion or rollback;
- duplicate retries and crash-after-land are exactly-once;
- mismatched base/overlay generations are rejected;
- promotion preserves operation/assertion identity;
- conflicts/orphans/quarantine are deterministic and visible;
- restore meets documented RPO/RTO.

Cutover: epoch-fence D1, seal an inclusive sequence/digest, import current rows, activate release-pinned writes, and retain legacy reads until parity.

### Phase 4 — remove routine generated snapshots from Git

Deliverables:

1. Publish generated JSONL, SQLite, cells, synapses, frontier, and shards as release artifacts.
2. Retain deterministic compatibility export commands.
3. Keep only small authority/manifests/schemas/reducer code/policy in normal Git history.
4. Provide local commands to fetch a release, build offline, and compute semantic diff.
5. Update contributor and agent documentation.

Acceptance:

- fresh clone plus one offline pack supports full query and validation without credentials;
- pull requests review small semantic changesets;
- old JSONL consumers can export equivalent files;
- repository growth and line churn decline materially.

### Phase 5 — optional PostgreSQL shadow projection

Start only after a PostgreSQL trigger is measured.

Acceptance before any dependency:

- 30 consecutive days of parity;
- empty and incremental roots agree;
- required queries meet targets;
- database can be disabled without breaking static/SQLite serving;
- isolated-generation restore drill succeeds.

### Phase 6 — optional R2 artifact serving

Start only after an R2 trigger is measured.

Acceptance:

- complete shadow-upload and dual-read digest parity;
- release/overlay CAS and rollback pass under cache load;
- missing/corrupt objects fail closed;
- prior release remains readable throughout transition.

### Phase 7 — measured derivation optimization

1. Replace or localize the quadratic layout at 30,000 cells or 30-minute layout p95.
2. Add incremental component rebuilds only where profiling supports them.
3. Partition compatibility exports before 75 MB and require action before 95 MB.
4. Reconsider a graph-native read projection only for a demonstrated workload.

## Verification

### Authority

- canonical encoding and hash fixtures;
- duplicate keys, Unicode, number, absent/null, and unknown-version rejection;
- first-parent and protected-path mutation rejection;
- landing-head race/revalidation;
- operation-ID reuse/idempotency;
- validator-derived conflict footprints;
- retract/restore/supersede state machine and cycle prevention;
- identity alias/merge/split behavior;
- reducer epoch and migration replay.

### Sources and reducer

- network-disabled clean-room builds;
- missing/substituted/incorrectly typed source pins;
- offline-pack closure;
- license/attribution propagation;
- provenance-only source transitions;
- snippet-loss regression based on the recent incident;
- full/incremental replay parity;
- checkpoint corruption/fallback.

### Releases

- missing artifact, wrong digest, and mixed release namespace;
- stale isolate/CDN cache behavior;
- canary failure before activation;
- concurrent activation attempts;
- rollback with newer overlay writes;
- incompatible overlay generation;
- retention and restore drill.

### Overlay

- old-base edits;
- cell split/merge endpoint remapping;
- identity alias/deletion;
- absorbed versus independent equivalent assertions;
- tombstone after promotion;
- duplicate idempotency key;
- concurrent retract/restore;
- crash between Git landing and D1 receipt;
- conflict/orphan/quarantine;
- D1 restore and Git reconciliation.

### Existing and new commands

Existing suites remain required:

```bash
cd /Users/jackmccarthy/projects/WikiLean
python3 -m unittest discover -s brain -p 'test_*.py'
python3 brain/test_acceptance.py
python3 brain/test_cells.py
python3 brain/test_cell_shards.py
python3 brain/test_frontier.py
python3 site/test_frontier_page.py

cd /Users/jackmccarthy/projects/WikiLean/wiki
npm run typecheck
npm run test:unit
npm run test:corpus
npm run test:e2e
npm run test:ci
```

Planned narrow contract commands:

```bash
cd /Users/jackmccarthy/projects/WikiLean
python3 brain/tools/validate_authority.py --base <trusted-commit> --head HEAD
python3 brain/tools/verify_source_set.py --manifest <source-set-manifest>
python3 brain/tools/replay_authority.py --offline-pack <pack> --network disabled
python3 brain/tools/semantic_diff.py --from <release-a> --to <release-b>
python3 brain/tools/verify_release.py --manifest <release-manifest>
```

## Operational targets

Record source acquisition/digests, semantic changes by source/kind, snippet changes, build/layout time and memory, artifact size/count, SQLite query latency, upload/activation time, rollback cache convergence, D1 conflicts/promotion retries, and repository/PR churn.

Initial targets:

| Objective | Target |
|---|---:|
| Public bounded-query p95 | under 500 ms |
| Nightly build p95 | under 30 minutes |
| Mixed-generation reads | zero; fail closed |
| Tombstone resurrection | zero |
| Failed canary production activation | zero |
| Initial rollback convergence | under five minutes, measured end-to-end |
| Offline rebuild | deterministic logical roots |
| D1 recovery | documented RPO/RTO proven by drill |

Do not claim a 30-second rollback until cache-convergence measurements support it.

## Explicit non-goals

This plan does not require:

- immediate PostgreSQL or R2 deployment;
- moving public reads behind a database;
- one operation per machine-derived edge;
- universal bitemporal history;
- automatic object garbage collection;
- graph-native primary storage;
- redesigning Brain v3 cell semantics;
- removing D1 immediate-live behavior;
- choosing a database vendor, ORM, hash algorithm, or signing hierarchy now.

## Recommended approval boundary

Approve **Phase 0 and the schema-design portion of Phase 1 only** as the first implementation slice.

That slice delivers:

1. the proposal-fold correctness fix and fatal nightly gate;
2. complete reducer-input inventory;
3. source-manifest and offline-pack schemas;
4. sealed source acquisition for the inputs implicated in the reproduction incident;
5. network-disabled clean-room replay;
6. canonical encoding/logical-root specification;
7. semantic snapshot diffing;
8. release-manifest and immutable-attestation schemas over current outputs;
9. proof that public static behavior remains unchanged.

It immediately improves correctness and reviewability without prematurely committing WikiLean to PostgreSQL, R2, or a production serving migration.
