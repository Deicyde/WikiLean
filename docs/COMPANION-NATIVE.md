# Native companion records in WikiLean

WikiLean's native application format stores complete Literature Links 2.0
annotations in D1. Normal native reads and edits use those objects directly. The
vendored schema and its pin are in `wiki/src/vendor/literature-links/`.
The current implementation is available for local testing; no production database
has been migrated or Worker deployed by this work.

## Before any deploy from this commit on

This Worker reads and writes `revisions.source_revid` and
`revisions.schema_version` on every save, revert and create, legacy (schema 3)
articles included; without those columns each of them answers 500. So before
any Worker deploy or Brain promotion of a release built from a commit that
contains `wiki/migrations/0014_native_companions.sql`:

1. Back up D1: `cd wiki && npm run backup:d1` (see `docs/BACKUP.md`).
2. Apply 0014 once (it is not idempotent):
   `cd wiki && npx wrangler d1 execute wikilean --remote --file=migrations/0014_native_companions.sql`.
3. Check that `SELECT name FROM pragma_table_info('revisions')` lists
   `source_revid` and `schema_version`, and that `companion_migrations` exists.

0014 converts no article: schema-3 articles stay legacy until the migration
below. Workers built before it ignore the new columns, so it can be applied
ahead of the release.

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
        "subject": [{"type": "generic", "uri": "https://example.org/article"}],
        "object": [{"type": "lean", "uri": "https://github.com/leanprover-community/mathlib4",
                    "location": {"declaration": "AddCommGroup", "module": "Mathlib.Algebra.Group.Defs"}}]
      }
    }
  ],
  "unlinked": [{"type": "generic", "uri": "https://example.org/other-article"}]
}
```

Only the `annotation` value is a Literature Links record, and each `unlinked`
entry is one Literature Links end beside an optional `base_uri`. D1's
`schema_version=4` identifies WikiLean's internal work-item storage format;
`articles.version` remains its optimistic concurrency and render-cache counter.
Neither changes the companion schema. Work items with no formal end keep their
source selections in `unlinked`, without inventing an object. Several anchors or
formal ends remain several links within one coverage item. Every subject end of
a link and every unlinked end is an occurrence of the item.

A link's annotation has at least one object end. Migration turns the legacy
Mathlib module and declaration into a `lean` object end (`uri` the Mathlib
repository, `location` with `declaration` and `module`, no `version`, because
legacy items never recorded the commit), so that information is stored on the
link itself rather than only in the private archive. A `noMatch` link, a checked
absence, is stored only when every object end has the `version` that was
searched. The editor shows a Mathlib declaration as its docs page; a docs page
entered there becomes a `lean` end, and any other reference a `generic` end. A
changed reference replaces the object and drops the stored `relation` and
`junk`, which described the old one.

The review item stays the editable authority for its facts. When a record leaves
WikiLean (`companionAnnotation` and `unlinkedAnnotation` in
`wiki/src/native-companion.ts`), those facts fill the format's own fields:

- item `status`, `kind`, `note` and `id` become the record's (stored values take
  precedence; `status` is always the item's), and `label` becomes each subject
  end's `title`;
- `match_kind` becomes the `relation`: `exact` is `exactMatch`, `generalization`
  `broadMatch`, `close` `closeMatch`, and `related` and `invocation`
  `relatedMatch`. `special_case` gives none, because the tags use it in both
  directions. For an item whose `status` is `not_formalized`, only
  `relatedMatch` is written. Without one of these, a stored `relation` is
  kept, and a stored `noMatch` always is;
- a subject end on the article's pinned revision is written with the article's
  readable URL as `uri`, the `oldid` as `version`, a CSL `webpage` citation whose
  `URL` is the pinned revision, and `identifiers.wikidata` when the article has
  a QID;
- `provenance`, and a `match_kind` that the relation does not name alone
  (`related`, `invocation`, or one with no relation), go under
  `extensions["org.wikilean"]`. Stored extensions are kept.

Each unlinked selection leaves as a record with `"object": []` and no
`relation`. WikiLean records no Mathlib commit, so a `not_formalized` item is
such a record with `"status": "not_formalized"`, not a `noMatch`. No retrieval
date is emitted: WikiLean stores none per article.

Internal link IDs are unique within an article and remain distinct from optional
Literature Links IDs. Duplicate Literature Links IDs and identical annotations are preserved as independent
occurrences. Both `links` and `unlinked` arrays are required, including when empty.
Live native items cannot also contain the old `anchor`, `anchors`, `mathlib`,
`decl` or `module` fields. An optional private `legacy` archive preserves original
migration evidence, not editable link authority.

## Validation and references

All five end types, `pdf`, `latex`, `html`, `lean` and `generic`, are validated
and stored. Unknown extensions and valid generic locations survive edits. An unsupported location can
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

The adapter also checks the reference PDF rectangle and line-order rules in every end,
and rejects strings with an unpaired UTF-16 surrogate. HTTP writes and migration
input use the platform JSON parsers, which keep the last of duplicated object keys.
Validation never retrieves a schema, source URI, or formal target. Arbitrary
lexically valid opaque references retain their exact strings.

Relative end or `$schema` references require an absolute `base_uri` on their
stored link envelope. Unlinked relative ends need their own base. Reads retain
that original carrier context; the SQLite filename and API URL never become
implicit bases. A base may be a file URI, but the Worker never serves
or reads that file.

The JavaScript storage boundary rejects integers outside
`[-9007199254740991, 9007199254740991]`, including inside opaque extensions and
private metadata, so it cannot silently persist rounded integer values. This is
an application limit, not a restriction in the Literature Links core schema. Native article
saves retain the existing work-item count, field-length and payload-size limits.

## Read and write contracts

- `GET /api/article/:slug/companion.jsonl` returns each active item's links and
  then its unlinked selections as Literature Links records, projected as above,
  one per line (`application/x-ndjson`). Rejected items are omitted for every
  caller.
  It returns 409 whenever a record has an explicit carrier base, including when
  all core references are absolute: an opaque profile may contain relative values.
- `GET /api/article/:slug/companion-records.json` returns
  `{version, records:[{item_id,id,annotation,base_uri?}]}` for native articles,
  with `unlinked` (the selection's index) in place of `id` for an unlinked one,
  with the same projection applied to each `annotation`. Nothing stored is
  removed, so the response keeps relative references and bases; it is not an
  annotation file.
- `GET /api/article/:slug.json` retains the application envelope. Anonymous reads
  use a native metadata allowlist and null tombstone placeholders. The pipeline
  bearer receives full work items so it can preserve protected data.
- `PUT /api/article/:slug` remains bot-only. An explicit `schema_version` (3 or 4)
  decides the storage format; without one, native work items create a native
  article and a legacy payload keeps temporary legacy compatibility. Native
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
Human veto tombstones retain their original source pins. A pin advance moves every
active article-local source, human-owned ones included: the pin is article metadata,
so human items are compared modulo that move and keep their human attribution, while
any other change to human bytes is still refused.

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

The additive SQL migration is `wiki/migrations/0014_native_companions.sql`; D1
gets it separately, before any deploy (see above). The
rehearsal applies it to an older copied database when needed, then performs all
article conversions in one transaction. For every converted article it:

1. Archives the exact old annotation string, pending/rejected proposal strings,
   old version and source pin in `companion_migrations`.
2. Preserves existing item IDs and assigns deterministic IDs only when absent.
   Each native item's private archive preserves its original pre-ID object.
3. Retains every work item, including vetoes, unknown metadata and targetless work.
   Invalid source context, IDs, statuses or unsafe numeric values fail the batch.
   Expansion beyond the Worker's save limits also fails, preserving an editable
   canonical state rather than publishing a snapshot that cannot be saved. The
   resolution pass below re-checks those limits on its own output.
4. Adds an observed pre-migration checkpoint with its known source revision, then
   a native snapshot. It leaves older history unchanged, including unknown pins.
5. Bumps the article content version and computes counts from final work items.
   Operational proposals stay intact; their IDs still refer to the same items.

The migrated copy still holds every WikiLean anchor in the profile form. The
cutover's second pass, run from the repository root with Python 3.12, resolves
them into Literature Links quotations and selectors against each article's
pinned Wikipedia HTML:

```sh
python3 site/resolve_sqlite.py /absolute/path/native.sqlite --report /absolute/path/resolution.json
```

It rewrites `articles.annotations` and the unserved `native-migration` revision
only, leaves `version` unchanged, and re-checks the Worker's save limits on the
resolved snapshot: an article that would exceed them keeps its profile
locations and is reported as skipped.

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

`companion.jsonl` and `companion-records.json` answer 409 for an article that
has not been migrated; there is no legacy conversion.
