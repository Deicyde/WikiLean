# Native companion records in WikiLean

WikiLean's native application format stores complete Formal Companion Annotation
(Literature Links) objects in D1. Normal native reads and edits use those objects directly.
The current implementation is available for local testing; no production database
has been migrated or Worker deployed by this work.

An article still owns review items with stable IDs, coverage status, provenance,
notes and moderation history. Each item contains:

```json
{
  "id": "aaaaaaaaaaaa",
  "status": "partial",
  "provenance": "ai",
  "links": [
    {
      "id": "aaaaaaaaaaaa:link:0",
      "annotation": {
        "source": {"type": "generic", "uri": "https://example.org/article"},
        "target": "https://example.org/formal#theorem"
      }
    }
  ],
  "unlinked": []
}
```

Only the `annotation` value is a Literature Links record. It has no version field. D1's
`schema_version=4` identifies WikiLean's internal work-item storage format;
`articles.version` remains its optimistic concurrency and render-cache counter.
Neither changes the companion schema. Work items with no formal destination keep
source/location selections in `unlinked`, without inventing a target. Several
anchors or formal destinations remain several links within one coverage item.

A link's `target` is a reference string or an endpoint object `{uri, pin,
extensions}`. Migration turns the legacy Mathlib module and declaration into a
`repository` pin on the target (`url`, `module`, `declaration`; no `commit`,
because legacy items never recorded one), so that information is stored on the
link itself rather than only in the private archive. A link must have a target;
the format also allows records without one, and WikiLean keeps those in
`unlinked`. The editor replaces a pinned target with the plain new reference when
its destination changes, since the pin cites the old declaration.

The review item stays the editable authority for its facts. When a record leaves
WikiLean, those facts fill the format's own fields: item `status`, `kind`,
`label`, `note` and `id` become the annotation's `status`, `kind`, `label`,
`note` and `id` (stored values take precedence; `status` is always the item's);
an occurrence whose source is the article's pinned Wikipedia revision gets a
`website` pin (`title`, `owner`, `url`, `revision`, `identifiers.wikidata` when
the article has a QID); `provenance` and `match_kind`, which have no format
slot, go under `extensions["org.wikilean"]`. Stored extensions are kept. The
same projection lives in `wiki/src/native-companion.ts` (`companionAnnotation`)
and `site/native_annotations.py` (`companion_annotation`). No retrieval date is
emitted: WikiLean stores none per article.

Internal link IDs are unique within an article and remain distinct from optional
Literature Links IDs. Duplicate Literature Links IDs and identical annotations are preserved as independent
occurrences. Both `links` and `unlinked` arrays are required, including when empty.
Live native items cannot also contain the old `anchor`, `anchors`, `mathlib`,
`decl` or `module` fields. An optional private `legacy` archive preserves original
migration evidence, not editable link authority.

## Validation and references

All four Literature Links modes—PDF, LaTeX, HTML and generic—are validated and stored. Unknown
extensions and valid generic locations survive edits. An unsupported location can
be displayed as a link without pretending its source passage was resolved.
Omitting `location` means the whole resource; an unresolved locator is never
converted to omission.

`wiki/src/vendor/literature-links/companion.schema.json` is an exact upstream schema copy.
`provenance.json` records its repository commit, SHA-256, and semantic-checker pin.
The generated validator is built offline with pinned Ajv/esbuild versions and
contains no runtime code generation. Regenerate or check it from `wiki/` with:

```sh
node scripts/generate-companion-validator.mjs
node scripts/generate-companion-validator.mjs --check
```

The adapter also checks the reference PDF rectangle and LaTeX line-order rules.
HTTP writes and migration input use a strict JSON decoder that rejects duplicate
object keys, including differently escaped spellings of the same key.
Validation never retrieves a schema, source URI, or formal target. Arbitrary
lexically valid opaque references retain their exact strings.

Relative source, target, or `$schema` references require an absolute `base_uri`
on their stored link envelope. Unlinked relative source selections need their own
base. Reads retain that original carrier context; the SQLite filename and API URL
never become implicit bases. A base may be a file URI, but the Worker never serves
or reads that file.

The JavaScript storage boundary rejects integers outside
`[-9007199254740991, 9007199254740991]`, including inside opaque extensions and
private metadata, so it cannot silently persist rounded integer values. This is
an application limit, not a restriction in the Literature Links core schema. Native article
saves retain the existing work-item count, field-length and payload-size limits.

## Read and write contracts

- `GET /api/article/:slug/companion.json` returns the stored public Literature Links objects,
  with the item facts and pins projected as above, as a discovery-compatible
  array. Rejected items are omitted for every caller.
  It returns 409 whenever a record has an explicit carrier base, including when
  all core references are absolute: an opaque profile may contain relative values.
- `GET /api/article/:slug/companion-records.json` returns
  `{version, records:[{item_id,id,annotation,base_uri?}]}` for native articles,
  with the same projection applied to each `annotation`. Nothing stored is
  removed, so the response keeps relative references and bases; it is not an
  Literature Links shard container.
- `GET /api/article/:slug.json` retains the application envelope. Anonymous reads
  use a native metadata allowlist and null tombstone placeholders. The pipeline
  bearer receives full work items so it can preserve protected data.
- `PUT /api/article/:slug` remains bot-only. New articles default to native format;
  an explicit `schema_version:3` selects temporary legacy compatibility. Native
  creation requires native work items and a pinned Wikipedia revision.
- `POST /api/article/:slug` retains existing authentication and `base_version`
  concurrency. Native articles reject legacy payloads. Old articles require an
  explicit migration before accepting native payloads; a save is not a migration.

Native editor state omits private archives and unfamiliar workflow fields.
`note` is public, as on legacy articles; `proof_note` stays editor-only.
The server restores those stored fields when the editor saves. Public historical
diffs involving native state apply the public metadata boundary to both sides,
including an older legacy snapshot. Conflict responses use editor-safe native
metadata for sessions; the authenticated pipeline can read full state.

Bot bulk writes cannot alter human-owned items or human veto tombstones. New bot
records cannot claim human provenance. Session edits derive provenance from the
authenticated actor; endorsement remains session-only. Native proposals can
replace `links` and `unlinked` with validated values. Applying a bot decision to
human-owned content records `ai-moderated` provenance. Existing proposals in the
old `mathlib` dialect remain archived/pending and require an explicit native rebase
or rejection; approval does not silently reinterpret them.

Article updates, counts, source pin, revisions, events and proposal bookkeeping
share the existing guarded D1 transaction. Counts continue to count work items,
not links. A source-pin advance checks every active article-local occurrence
against the new Wikipedia HTML. Other imported sources are retained independently.
Human veto tombstones retain their original source pins. A bot cannot bypass human
preservation by changing source URIs; a moderator can explicitly save a coherent
reanchored native snapshot with `revid` and `base_version`, with human attribution.

## Copied-database migration rehearsal

Use a deliberately selected SQLite snapshot, never the annotation sidecar cache.
The command uses SQLite's backup API to capture a consistent copy, modifies only
that copy, checks its integrity and publishes to a new filename. It makes no
network request and cannot execute D1 operations.

From `wiki/`, using Node 22 and installed dependencies:

```sh
npx tsx scripts/migrate-native.ts /absolute/path/input.sqlite /absolute/path/native.sqlite
```

The output must not already exist and must differ from the input. A failed batch
publishes nothing. The input is opened read-only. A successful report lists each
article's item, link and unlinked counts and diagnostics, including status/target
contradictions, missing targets and unresolved legacy anchors.
Only application schemas 3 and 4 are recognized; unknown formats fail closed.

The additive SQL migration is `wiki/migrations/0013_native_companions.sql`. The
rehearsal applies it to an older copied database when needed, then performs all
article conversions in one transaction. For every converted article it:

1. Archives the exact old annotation string, pending/rejected proposal strings,
   old version and source pin in `companion_migrations`.
2. Preserves existing item IDs and assigns deterministic IDs only when absent.
   Each native item's private archive preserves its original pre-ID object.
3. Retains every work item, including vetoes, unknown metadata and targetless work.
   Invalid source context, IDs, statuses or unsafe numeric values fail the batch.
   Expansion beyond the Worker's save limits also fails, preserving an editable
   canonical state rather than publishing a snapshot that cannot be saved.
4. Adds an observed pre-migration checkpoint with its known source revision, then
   a native snapshot. It leaves older history unchanged, including unknown pins.
5. Bumps the article content version and computes counts from final work items.
   Operational proposals stay intact; their IDs still refer to the same items.

Repeated migration of an already native database validates it and leaves its
article versions and history unchanged. The library entry point
`migrateLocalDatabase` supports hermetic tests and explicit local fixtures.

Legacy seed builders reject native sidecars. ID backfill and seed-refresh SQL
exclude native rows at read and write time. Count backfill supports native work
items and guards its derived update against a changed article version. These tools
do not replace the canonical native API and are not authorization to reseed D1.

## History and rollback

New revisions store `source_revid` and the application `schema_version` beside
their complete annotation snapshots. Individual revert and run rollback restore
the native state and source pin in the same guarded update. A native restore from
old history with an unknown source revision fails closed; it never assigns the
current pin to old source selections. The migration's observed checkpoint provides
a known-context restore point without altering older evidence.

Run rollback preserves edits and human proposal approvals made after the affected
run. Native snapshots can be restored after subsequent native edits with the
usual authorized revert workflow. Rolling back software requires a reader that
understands native snapshots. Restoring the original input database would discard
new edits and is not the rollback procedure.

The legacy companion conversion endpoint remains only for explicitly unmigrated
articles during compatibility testing. Native articles and the local workspace's
normal path read stored associations directly. See
[the native design](COMPANION-NATIVE.plan.md) and
[the local workspace guide](COMPANION-WORKSPACE.md).
