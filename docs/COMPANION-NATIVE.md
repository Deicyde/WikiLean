# Native companion records in WikiLean

WikiLean's native application format stores complete Literature Links 1.0
annotations in D1. Normal native reads and edits use those objects directly. The
vendored schema is pinned to Literature Links 1.0.2; its provenance is in `wiki/src/vendor/literature-links/`.
Merging this code neither migrates production data nor deploys a Worker. Record
live completion only after the operator verifies the deployed release and audits
the canonical D1 state; a local rehearsal is not a production receipt.

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
Mathlib declaration into a `lean` object end (`uri` the Mathlib repository,
`location` with `declaration` and, when known, `module`; no `version`, because
legacy items never recorded the commit). A declaration with no module remains
an active target, including Lean core names; its reference uses the repository
URI. The link stores this information beside the exact private archive.
A `noMatch` link, a checked
absence, is stored only when every object end has the `version` that was
searched. The editor shows a Mathlib declaration as its docs page; a docs page
entered there becomes a `lean` end, and any other reference a `generic` end. A
changed reference replaces the object ends, and the junk values each of them
listed go with them; it also drops the stored `relation`, which described the
old object. Junk values belong to an end (Literature Links rejects a
record-level `junk`). WikiLean writes none itself and keeps those on imported
ends, except that a source selection moved to other text drops its own.

The review item stays the editable authority for its facts. When a record leaves
WikiLean (`companionAnnotation` and `unlinkedAnnotation` in
`wiki/src/native-companion.ts`), those facts fill the format's own fields:

- item `status`, `kind`, `note` and `id` become the record's (stored values take
  precedence; `status` is always the item's), and `label` becomes each subject
  end's `title`;
- `match_kind` becomes the `relation`: `exact` is `exactMatch`, `generalization`
  `broadMatch`, `close` `closeMatch`, and `related` and `invocation`
  `relatedMatch`. `special_case` gives none, because the tags use it in both
  directions. Without one of these, a stored `relation` is kept, and a stored
  `noMatch` always is. An item whose `status` is `not_formalized` has no
  declaration that states it, so its links carry `relatedMatch`, `noMatch` or
  no relation: any other relation, from `match_kind` or stored on the link, is
  not written;
- a subject end on the article's pinned revision is written with the article's
  readable URL as `uri`, the `oldid` as `version`, a CSL `webpage` citation whose
  `URL` is the pinned revision, and `identifiers.wikidata` when the article has
  a QID; every end keeps its own `junk`;
- `provenance`, and a `match_kind` that the relation does not name alone
  (`related`, `invocation`, or one with no relation), go under
  `extensions["org.wikilean"]`. Stored extensions are kept.

Each unlinked selection leaves as a record with `"object": []` and no
`relation`. WikiLean records no Mathlib commit, so it writes no `noMatch` of
its own: a `not_formalized` selection leaves as such a record. No retrieval
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

It fetches each pinned revision from Wikipedia through `render.py`'s cache
(`--cache` for another directory). It rewrites `articles.annotations` and the
unserved `native-migration` revision only, leaves `version` unchanged, and
re-checks the Worker's save limits on the resolved snapshot: an article that
would exceed them keeps its profile locations and is reported as skipped.

Repeated migration of an already native database validates it and leaves its
article versions and history unchanged. The library entry point
`migrateLocalDatabase` supports hermetic tests and explicit local fixtures.

Legacy seed builders reject native sidecars. ID backfill and seed-refresh SQL
exclude native rows at read and write time. Count backfill supports native work
items and guards its derived update against a changed article version. These tools
do not replace the canonical native API and are not authorization to reseed D1.

## Live migration procedure

This procedure is separate from the R2 retirement of the old article plane. It
does not enable editing there, retire any URL, or authorize R2–R6. The endpoint
below is implemented but is unavailable until a release containing it is
promoted. No production migration is implied by merging this code.

Use the authenticated operator host and the exact release process in
[the release runbook](BRAIN-RELEASE-RUNBOOK.md). Retain the frozen release, public
baseline, fresh P1B evidence and external journal; obtain Jack's approval for
those exact identities and the exclusive migration window. A checkout with no
Cloudflare login or retained assets cannot stand in for that host. Do not use a
direct Worker deploy or restore an entire modified SQLite database into D1.

1. Identify and stop the installed operator moderation service and any other
   pipeline writers. Record their previous state so only previously running jobs
   are resumed. Hold the release journal's exclusive operator window. Set
   site_settings.site_write_mode to locked, retaining its previous value.
   This brakes community/agent writes; it does not stop the pipeline bearer.
   Journal the drift Cron Trigger's exact schedule in the approved window. In
   Cloudflare's Workers dashboard, select this Worker, open Triggers, and remove
   each recorded Cron Trigger through its menu. Allow the documented propagation
   interval (up to 15 minutes), then confirm in Workers Logs that every in-flight
   scheduled invocation has finished before taking the snapshot. This is a
   temporary operator maintenance action: do not deploy an edited local config.
   [Cloudflare's trigger procedure](https://developers.cloudflare.com/workers/configuration/cron-triggers/#remove-a-cron-trigger)
   documents removal and propagation. Do not rely on a quiet site as evidence of
   a freeze.
2. Create and verify the encrypted D1 backup from docs/BACKUP.md, then acquire a
   fresh consistent snapshot. Keep it private. Verify 0014's columns and table,
   and the release's other migration prerequisites, against the live database;
   apply only the numbered files actually missing, once, after the backup.
3. Rehearse the structural conversion on that snapshot with migrate-native.ts
   above. Resolve each reported invalid item explicitly against live D1; never
   substitute the disk cache or coerce an unknown status. The old cached
   Students_t-distribution item with not_applicable is a review lead, not
   authority to delete or relabel today's record. Retake the snapshot and repeat
   rehearsal after any correction.
4. Review the item/link/unlinked totals, diagnostics, human provenance and
   tombstones, exact archives, proposal bytes, counts, source pins and new history
   against the rehearsal. Legacy special_case direction remains unknown;
   migration invents neither its direction nor a Mathlib commit or noMatch.
   Profile locations are retained without inventing quotations. The optional
   source-resolution pass above is a separate, reviewed transformation; it is not
   automatically applied by this structural endpoint.
5. Promote the approved exact release through the promoter. Verify its deployed
   source identity before making the first migration request. Recheck the Cron
   Triggers afterwards: a promotion can reinstall the configured schedule. If it
   does, repeat the temporary removal and propagation/in-flight checks before
   taking the final snapshot, rehearsal and first migration preview.

The live path accepts one article at a time from a signed-in **admin session**:
POST /api/admin/article/:slug/native-migration. It requires a same-origin
Origin header and a small JSON options body; pipeline and agent tokens cannot
call it. An empty options object previews without writing. It returns
base_version, source_revid, an opaque snapshot digest and report. Record those
exact values in the private migration journal and review diagnostics before
applying.

To apply, send only those three returned values and the boolean option apply:
true. Supplied annotation data is rejected. The community write brake must be
locked. The server recomputes the same conversion as the rehearsal, then atomically:

- archives the exact old annotation and pending/rejected proposal strings;
- inserts the observed source-pinned checkpoint and native revision, attributed
  to the administrator;
- updates the article, coverage counts and content version while retaining its
  source revision, item identities, human attribution and veto tombstones;
- advances an already-current moderation review stamp without deciding proposals.

The batch checks the original bytes, version, schema, Wikipedia title, source pin,
proposal strings, review stamp and latest revision, and checks that the write
brake is still locked. A changed or deleted article, concurrent proposal,
history change or unlocked brake returns 409 with no migration side effects.
Preview again and investigate the writer; do not retry automatically. A failed
SQL statement rolls the complete batch back. An already-native article is
validated and leaves its version/history unchanged.

After each apply, read /api/article/:slug.json and
/api/article/:slug/companion-records.json, validate the stored records, compare
the journaled totals and source pin, and verify archives and checkpoints in D1.
Use companion-records.json for relative records; standalone companion.jsonl
correctly refuses them without their carrier bases. Refresh the canonical D1
mirror and its derived citation, graph and autocomplete assets. Their readers
now handle every Mathlib target, exclude other libraries and preserve the
distinction between a work item and its several links. Current-checkout sweeps
and benchmark oracles defer version/hash-pinned targets, and automatic proposal
resolution leaves native rewrites pending for per-target review.

The sealed Brain reducer keeps its small declaration projection inside
brain/build_common.py, within every declared reducer scope. An isolated import
test copies only those declared files; parity fixtures check the application
reader's native, legacy, relative, pinned and excluded targets. The application
and derived-catalog normalizers retain their reviewed helper closures.

New source exports need a reviewed current normalizer profile and fresh source
identities. Verification retains each older reviewed profile's exact helper list
and original annotation semantics; a pre-native producer cannot consume native
annotations. Old source bundles and paid proof receipts are not relabeled as
products of the new code.

Close the migration window only after the live audit and remaining schema-3
count match the approved scope. Restore the recorded brake/cron/job state
deliberately, including recreating the exact journaled Cron Trigger schedule
through the dashboard and verifying it; leave slug_editing off. If an article
fails, leave it unchanged
and journal it. Report partial progress as partial, with the remaining slugs.

## History and rollback

New revisions store `source_revid` and the application `schema_version` beside
their complete annotation snapshots. Individual revert and run rollback restore
the native state and source pin in the same guarded update. A native restore from
old history with an unknown source revision fails closed; it never assigns the
current pin to old source selections. The migration's observed checkpoint provides
a known-context restore point without altering older evidence.

Run rollback preserves edits and human proposal approvals made after the affected
run. Native snapshots can be restored after subsequent native edits with the
usual authorized revert workflow. Structural rollback uses the same administrator
endpoint with the boolean option rollback: true. Preview it first, then send the
returned base_version, source_revid and snapshot with rollback: true and apply:
true while community writes remain locked. Only the unchanged article immediately
after its migration qualifies: its version, latest migration revision and
checkpoint, source pin, proposal strings and reconstructed native conversion must
still match the archive. The batch guards the archive too. Any subsequent edit,
history entry, proposal delta or mismatched archive refuses rollback; preserve
later contributions with explicit per-item corrections instead.

A successful structural rollback archives the native bytes and creates new
checkpoint/rollback revisions, restores the exact original legacy annotation
string, recomputes counts and increments the content version again. It does not
delete migration history, revert the version counter or restore a whole database.
Keep both readers available and verify the result before reopening writes.
Rolling back software requires a reader that
understands native snapshots. Restoring the original input database would discard
new edits and is not the rollback procedure.

`companion.jsonl` and `companion-records.json` answer 409 for an article that
has not been migrated; there is no legacy conversion.
