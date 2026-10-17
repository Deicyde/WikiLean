# The formalization atlas

**Status (2026-10-17): built on branch `claude/formalization-atlas`, not deployed.** The
Worker code, assets and migration 0015 are release-affecting and ship only in a release
(section 11). Nothing is uploaded anywhere. The decisions this needs from Jack are in
section 14.

The atlas answers one question per passage of a WikiProject Mathematics article: **is this
formalized in Lean, in which library, at which commit, and how closely?** The answers
come from the formal-companion skill's locate pipeline
(`docs/proposals/wikipedia-locate.md` in that repository, "the proposal" below), which
exports sealed sets of Literature Links 2.0 records. WikiLean receives them as a separate,
read-only data plane, as proposal §11.2 asks.

Two names to keep apart:

- This is not the old graph-stack "atlas" (`/atlas`, `/atlas_data.json`, `/api/atlas` and
  an older `wiki/src/atlas.ts`), retired 2026-07-10 and deleted 2026-08-04. Older documents
  such as `docs/BRAIN.md` describe that one. The old bare `/api/atlas` and
  `/api/atlas/<slug>` paths still answer 404.
- It is not facebookresearch's ATLAS Lean library (`atlas-lean`). That library appears
  here only as a universe library, and the proposal's example universe excludes it because
  its licence forbids ML evaluation use.

## 1. Why a separate plane

WikiLean's per-article D1 annotations are the human-moderated layer, and their invariants
are load-bearing (CLAUDE.md, "Hard invariants"). Locate output differs in kind: tens of
thousands of passages, several libraries per passage, commit pins, checked absences. It
also arrives in bulk from a pipeline that WikiLean does not run. Writing it into D1 now
would collide with every invariant at once. The plane avoids that:

| D1 annotation path | Atlas plane |
|---|---|
| Writes `articles.annotations` and bumps `version` | Never writes annotations; its only D1 table is `atlas_feedback` |
| 256 KB cap per article (`MAX_ANNOTATIONS_BYTES`) and 500 items | No cap: one static object per article |
| `EDIT_LIMITER` on every write, 30/min | Reads are unlimited; only feedback writes take the limiter |
| Item identity: 12-hex ids, `findMatch` re-pairing, `findLostHuman` 422 | No identity coupling: keyed by Wikipedia page id and the producer's `location_key` |
| The nightly moderation job reviews and may rewrite items | The nightly never reads or writes the store, so it cannot overwrite it |
| `articles.revid` advances only with re-anchored annotations | The store pins its own revision per article and never moves `articles.revid` |
| Native Literature Links storage waits on migration 0014 | Works on legacy schema-3 production: it needs no annotation migration |

The D1 import is a later, gated step (section 12).

## 2. Data flow

```
formal-companion locate pipeline (fleet host)          WikiLean
  L0-L6: universe, sources, inventory, locate,
         gate, challenge, export
            |
            v
  sealed record set  ----(operator copies)---->  scripts/atlas/build_store.py
  (SEAL.json + files)                                 | validates, then writes
                                                      v
                                                 store tree atlas/v1/...
                                                      | operator uploads (section 6)
                                                      v
                                                 R2 bucket, bound as ATLAS
                                                      | read-only
                                                      v
                       Worker: /atlas, /atlas/:pageid, /api/atlas/*, article-page box
                                                      |
                       signed-in readers: Confirm / Report  -->  D1 atlas_feedback
                                                      |
                       pipeline bearer: GET /api/atlas/feedback/export  -->  benchmark seeds
```

## 3. Input contract: the sealed record set

The layout follows the proposal §11.1 and the skill's `references/locate-documents.md` §6:

```
<set>/SEAL.json                      the seal (below); lists every other file with its sha256
<set>/universe.json                  the universe manifest (universe.yaml is accepted too, not both)
<set>/sources.jsonl                  one row per source (article revision)
<set>/records/<source-id>.jsonl      that source's Literature Links 2.0 records
<set>/receipts/<sha256>.json         search receipts, named by the sha256 of their bytes
```

`SEAL.json`:

```json
{"schema": "cf-locate-recordset/1",
 "campaign": "<token>",
 "universe": {"id": "u1", "sha256": "<64 hex>"},
 "sourceset": {"id": "<id>", "manifest_sha256": "<64 hex>"},
 "skill_commit": "<40 hex>",
 "created": "2026-10-17T12:00:00Z",
 "files": {"<relative path>": "<sha256>", ...}}
```

`universe.json`: `id`, `frozen` (YYYY-MM-DD), `universe_sha256`, `libraries` (each with
`id`, `repo` (https), `commit` (40 hex), and optionally `display_name`, `brain_lib`,
`toolchain`, `roots`, `licence: {spdx}`, `src_dir`, `blob_url_template`), and
`excluded` (each `{id, reason, repo?}`), as in proposal §2.3.

`sources.jsonl`: `source_id` (`enwiki-<pageid>-<revision>`), `pageid`, `revision`, `title`,
`wikidata`, `html_sha256`, `layer_sha256`, `renderer`, and `assessment: {class, importance}`
(the WikiProject assessment, or null).

### What the builder checks

`scripts/atlas/build_store.py` refuses the whole set, listing every problem it found, when
any of these fails:

1. **Seal.** `SEAL.json` has the fields above; every listed file exists and matches its
   sha256; no unlisted file, symlink or unsafe path is present. Receipts are named by
   their own sha256, and every receipt is referenced.
2. **Universe.** Its id and `universe_sha256` equal the seal's; library ids and
   repositories are unique; commits are 40-hex.
3. **Sources.** `source_id` agrees with `pageid` and `revision`; there is one revision per
   page per set; every source has a records file, and every records file has a source.
4. **Schema.** Every record passes the vendored Literature Links schema and its numeric
   checks, through the checker WikiLean already uses: `site/native_annotations.validate_link_record`
   on `wiki/src/vendor/literature-links/companion.schema.json`, whose sha256 is checked
   against `provenance.json` first. JSON with duplicate keys or NaN is refused.
5. **Records (proposal §2.7).** Each record carries the `org.mathlib-initiative.formal-companion.locate`
   extension with `item` (`In-text #<8-16 hex>`), the set's universe id and sha, and a
   `challenge` value; `id` is `<source_id>::<item>`. The subject is one `html` end at the
   source's revision, with `pageid` and `html_sha256` matching `sources.jsonl` and a quote
   within the export caps (12 words of `exact`, 32 characters of prefix and of suffix).
   - A **location** (relation `exactMatch`, `closeMatch`, `broadMatch`, `narrowMatch`,
     `hasPart` or `relatedMatch`) has one `lean` end at its library's repo and commit, with
     `declaration`, `module`, a safe `.lean` `path`, `1 <= line <= end_line`, a
     `proof_status`, a unique 64-hex `location_key`, `rank` 1-5 and a `confidence`. A
     `closeMatch` names its convention difference in `note`.
   - An **absence** (`noMatch`) has status `not_formalized` and exactly one whole-library
     `lean` end per universe library, and references its receipt as
     `receipt: {sha256, queries, candidates}`. The counts must match the receipt file, and
     the receipt must list at least 3 distinct queries (gate rule 5).
   - An **unresolved** passage is a single record with `object: []`, no status and no
     relation.
6. **Passages (proposal §2.6).** The records of one passage agree on kind, quote and status.
   A passage has at most one absence and at most 5 locations, with distinct ranks. An
   absence sits only beside `relatedMatch` locations. The derived status meets its
   necessary conditions: a formalized theorem has an exact, close or broad match to a
   `proved` declaration; a formalized definition has an exact or close match to a
   `definition`; `statement_only` has an exact, close or broad match; `partial` has a
   hasPart, narrow or broad match; `not_formalized` has a checked absence. The builder
   checks these conditions and never recomputes the status. Computing it is the
   producer's job.

### Contract notes for formal-companion

These are where the builder departs from, or adds to, the proposal as written. Each
needs the skill's agreement before real record sets arrive:

1. **CSL citations need an `id`.** The vendored schema requires `citation.id` and
   `citation.type`. Proposal §2.7's example citation has no `id` and fails validation as
   written. The fixture uses the source id.
2. **`section` and `prominence` are requested in the record's locate extension**
   (optional: `section` is a list of heading strings, `prominence` is
   `subject|central|supporting`). The coverage page shows them, but §2.7's record shape
   carries neither, and the sealed set has no passage table. The "Gadget (mathematics)"
   fixture omits them, and the page copes.
3. **Absence receipts.** The proposal says "referenced by sha256 and count". The builder
   reads that as `receipt: {sha256, queries, candidates}` and checks both counts against
   the file.
4. **`universe_sha256` and `location_key` are opaque.** The builder checks that SEAL,
   universe and every record agree on the universe sha, and that location keys are 64-hex
   and unique. It does not recompute either, because the formulas belong to the producer
   (`location_key` uses the skill's private `clean()`).
5. **One revision per page per set.** The store is keyed by page id, so a set carrying two
   revisions of one page is refused.

## 4. The store (v1)

```
atlas/v1/current.json                    campaign, universe summary, totals, build time, index shard list
atlas/v1/manifest.json                   every other file: bytes and sha256
atlas/v1/universes/<universe-sha>.json   libraries (id, display name, repo, commit, toolchain, licence), exclusions
atlas/v1/articles/<pageid>.json          the article's view model (below)
atlas/v1/records/<pageid>.jsonl          that article's records, byte for byte as sealed
atlas/v1/index/<shard>.json              title -> page id and summary counts, sharded
```

- **Article view model** (`wikilean.atlas.article/1`): `pageid`, `title`, `revision`,
  `source_id`, `wikidata`, `assessment`, `campaign`, `universe {id, sha256, frozen,
  libraries}`, `record_set` (the SEAL's sha256), `records_sha256`, `summary` (passages,
  locations, related, by status, by library, by proof status, by relation) and
  `passages`. Each passage has `item`, `kind`, `section`, `prominence`, `quote`,
  `subject_uri`, `outcome` (located, absent or unresolved), `status`, and `locations`,
  each with `library, repo, commit, declaration, module, path, line, end_line, relation,
  proof_status, rank, confidence, challenge, location_key, note`. A passage also carries
  `absence {universe, universe_sha256, libraries, receipt, challenge}` or `unresolved
  {challenge}`. A `relatedMatch` states something else (proposal §2.6), so it is counted
  in `related` and never as a location.
- **Index shards.** The key is the first hex digit of `sha256(title_key)`, which gives 16
  shards. `title_key` is the NFC title with underscores and Unicode spaces collapsed to
  one space and trimmed, the same in Python (`title_key`) and TypeScript (`titleKey`). A
  test in each language pins the same shards. Rows are arrays named by the shard's
  `fields` list. A shard over 1 MiB is refused; a wider key would be a v2 store.
- **Determinism.** Output is canonical JSON (sorted keys, compact, UTF-8, newline) in a
  stable order: passages in record order, locations by rank, index rows by title.
  `built_at` defaults to SEAL's `created`, so the same set always builds the same bytes.

## 5. Building

```bash
python3 scripts/atlas/build_store.py --record-set <set> --out <store>            # new directory only
python3 scripts/atlas/build_store.py --record-set <set> --out <store> --check    # compare, write nothing
python3 scripts/atlas/build_store.py --record-set <set> --out <store> --replace  # rebuild in place
```

Exit codes: 0 is success, 1 means `--check` found differences, and 2 means the record set
was refused (the reasons go to stderr). The tree is staged next to `--out` and moved into
place, so a failed build leaves nothing half-written. `--built-at <ISO-Z>` overrides the
build time (pass the same value to `--check`). `scripts/` is release-neutral, so the
builder can change without a release.

The fixture lives in `scripts/atlas/fixtures/`. `make_fixture.py` writes `recordset/` (two
synthetic articles that cover every outcome) and `store/` is its built store. The Python
test pins both. The Worker tests and the browser harness read that same `store/`.

## 6. Store backend and publishing

The Worker reads through a one-method interface, `AtlasReader.text(key)`
(`wiki/src/atlas.ts`), which has four implementations:

| Reader | Backing | Wired today |
|---|---|---|
| `r2Reader` | R2 bucket bound as `ATLAS` | yes, when `ATLAS` is bound |
| `kvReader` | a KV namespace with the same keys | no (one-line change in `atlasReader`) |
| `staticReader` | the tree copied under `wiki/public/atlas-store/`, served by `ASSETS` | no |
| `memoryReader` | an in-memory map | tests and previews |

**The binding is not in `wrangler.jsonc`**, by design: the bucket name is Jack's choice.
To bind it, create the bucket and add:

```jsonc
"r2_buckets": [
  { "binding": "ATLAS", "bucket_name": "<chosen-name>" }
]
```

`src/wikipedia.ts` notes that **R2 was not enabled on the Cloudflare account** when the WP_HTML
cache was built. Enabling R2 is an account action in the dashboard, and it may ask for a
payment method. The free tier (10 GB, 1M writes and 10M reads a month) covers the atlas
many times over.

**Publishing** is an operator action, never the nightly and never the fleet host:

1. Build and `--check` the store from the sealed set on the operator machine.
2. Upload every object except `current.json` (`universes/`, `records/`, `articles/`,
   `index/`, `manifest.json`), keyed by its path. Universes are content-addressed, and
   each article object is self-contained, naming its own campaign, universe and record
   set.
3. Upload `current.json` last, then delete objects the new manifest no longer lists.

While an upload is running, readers can see a newer article next to an older index. Each
article stays internally consistent, so the worst case is a short-lived 404 from the index
for a newly added page. No upload script exists yet, deliberately: it is a remote write,
and who runs it is an open decision (section 14).

## 7. Worker routes

| Route | Answer | Cache-Control |
|---|---|---|
| `GET /atlas[?q=]` | dashboard: totals by status, by library, by WikiProject importance; article list, searched server-side through the index shards (200 rows shown) | none (HTML, like other pages) |
| `GET /atlas/:pageid[/:slug]` | one article's coverage page; the slug is cosmetic | none |
| `GET /atlas/universe/:sha` | the universe's libraries, commits and exclusions | none |
| `GET /api/atlas/current` | `current.json` | `public, max-age=60` |
| `GET /api/atlas/article/:pageid` | the view model | `public, max-age=300` |
| `GET /api/atlas/records/:pageid` | the raw records, `application/x-ndjson` | `public, max-age=300` |
| `GET /api/atlas/universe/:sha` | the universe | `public, max-age=86400, immutable` |
| `GET /api/atlas/index/:shard` | one index shard (used by the dashboard's live filter) | `public, max-age=300` |
| `POST /api/atlas/feedback` | section 10 | none |
| `GET /api/atlas/feedback/export` | section 10 | `no-store` |

With `ATLAS` unbound, every route above except the export answers **503 "atlas store not
configured"** (an HTML page or JSON, with `no-store`). A bound but empty store answers 503
"atlas store has no published build". An unknown or malformed page id, sha or shard
answers 404. `atlas` was already in `RESERVED`, so no article can take the name. The
security middleware (`src/security.ts`) adds its headers to every one of these responses.

## 8. UI

The UI is server-rendered (`wiki/src/atlas-render.ts`) with one small vanilla enhancement
(`site/assets/atlas.js`) and one stylesheet (`site/assets/atlas.css`), both copied by
`wiki/scripts/build-public.ts`. The pages work without JavaScript, except Confirm/Report
and the live list filter.

- **Coverage strip.** Passages checked and Lean locations, a coverage bar whose segments
  are filled with patterns (solid, hatched, dotted, cross-hatched, empty), and filter
  chips by status and by library. A chip with a zero count is shown but inert. A library
  with only related declarations is labelled "related only".
- **Passage block.** The kind and prominence in a small heading, the section path, and a
  status pill whose tooltip is a full sentence (also present as screen-reader text, for
  example "Formalized: Mathlib states this exactly (proved)."). Then the quote with its
  context, and a link to the passage in the pinned Wikipedia revision (a text fragment).
- **Location card.** One card per location: library chip, `@<sha7>`, the relation in
  words (exact match, close match, more general, special case, states part, related), and
  a proof-status badge. `statement only`, `wanted` and `axiom-dependent` are drawn as red
  outlined alerts and are never hidden. Then the declaration (namespace faint, leaf bold,
  wrapping at `.` and `_`), its module and lines, a convention note for close matches,
  the provenance line (rank, confidence, challenge), and "Source @<sha7>" linking
  `<repo>/blob/<commit>/<path>#L<line>-L<end_line>`.
- **Absence:** "Searched N libraries in universe <id> (<frozen>) — not found", linking the
  universe page, with the receipt's counts. **Unresolved:** "Undecided", never counted as
  not formalized.
- **Libraries** (`wiki/src/libraries.ts`). The universe entry's `display_name`,
  `blob_url_template` and `src_dir` win. Then come the seeds (Mathlib, Lean core with
  `src/`, Batteries, TauCeti, Formal Conjectures, atlas-lean), then the FormalFrontier
  organisation pattern ("FormalFrontier · <repo>"), then any GitHub repository. A
  non-GitHub repository without a template gets no source link rather than a guessed one.
- **Accessibility.** Status is never shown by colour alone: each status has an icon, a word
  and its own stroke (solid, dashed, dotted, double, dashed grey). Chips are real
  buttons with `aria-pressed`, and filter results are announced through a status region.
  The layout has 16px gutters on phones and no horizontal scroll. Colours are the
  stylesheet's tokens, so dark mode follows the site toggle and the OS preference. The
  browser soak (`wiki/e2e/atlas.spec.ts`) runs axe on the coverage page and dashboard in
  light and dark mode, and on an article page with the box. It also drives the filters,
  the feedback flow and the live search.
- **Security.** All store text is escaped (`htmlEscape`). Links are built from validated
  parts or parsed and required to be https, and paths with `..`, absolute or empty
  segments get no link. The pages have no inline event handlers, and `atlas.js` writes
  only through `textContent` and `createElement`. The pages keep two inline scripts
  byte-identical to the other shells (the theme bootstrap and the header search), and the
  coverage bar uses `style="width:…"`. Both are allowed by today's Report-Only CSP, and a
  strict CSP would need hashes for the scripts and `style-src-attr` for the bar.

## 9. Article pages

When `ATLAS` is bound and the article's Wikipedia title has an index entry, the article
page shows a compact box above the body: passages checked, counts by status, and "See
where →" linking `/atlas/<pageid>/<slug>`. `serveArticle` adds it **per request**, like
the staleness banner (`pages.ts injectAtlasSummary`), and never bakes it into
`render:v18`. The atlas changes independently of `articles.version`, so a cached box
would be stale for up to 30 days. The cached base bytes do not change, so **no render-cache
key bump is needed**, and the legacy annotation rendering is untouched.

The box costs one R2 read (one index shard) per article view when bound, and nothing when
unbound. A store failure is logged and drops the box; it never breaks the page. The match
is by title, because D1 stores no page id, so a page renamed after the atlas build loses
its box until the next build.

## 10. Feedback, and how it becomes benchmark seeds

**Table** (`wiki/migrations/0015_atlas_feedback.sql`): `id, location_key, pageid,
revision, campaign, verdict ('confirm'|'report'), note (at most 500 characters), user_id,
created_at, updated_at`, with `UNIQUE (location_key, user_id)`. A second verdict replaces
the first.

**`POST /api/atlas/feedback`** takes `{location_key, pageid, campaign, verdict, note?}`.
It answers 503 when unbound, 403 for a cross-origin request (`checkOrigin`), 401 without a
session, and 403 for the pipeline bearer (these rows are human labels). It is
rate-limited per user on `EDIT_LIMITER` (key `atlas:<user>`, 429). Bodies over 4 KB get
413 before parsing. It answers 400 for a bad field and 404 unless the location is on the
current store article. A campaign other than the store's gets **409** "stale", so a verdict
always names what the person saw. `revision` and `campaign` are copied from the store, not
taken from the client. Until migration 0015 is applied, the route answers 503, not 500.

**`GET /api/atlas/feedback/export`** is for the pipeline bearer only (admin sessions get
403). It returns every row as NDJSON, paged from D1, with users only as
`sha256(user_id + PIPELINE_TOKEN)[:12]`, the research export's pseudonym scheme. The
note is the only free text. The page tells signed-in readers that maintainers read
notes. The export reads only D1, so it keeps working if the store is unbound.

**Seeds.** Proposal §8.1 makes the maintainer's spot checks tier **G4** (up to 30 negatives
and 20 positives per release). Consumer-side human input is never a gold tier of its own.
Feedback rows are G4 **candidates**: a `report` is a candidate negative and a `confirm` a
candidate positive. The maintainer adjudicates them before any enters the gold manifest.
A row joins its sealed record set by `(location_key, campaign)`, which recovers the exact
relation and declaration judged; the relation is not part of `location_key`, so a verdict
on one campaign does not carry over to a re-judged relation. Per §8.4, the export stays
on the operator machine and never goes to the fleet host, and val/test seeds never feed
the improvement loop.

## 11. Release coupling

| Path | Class | Ships |
|---|---|---|
| `docs/ATLAS.md`, `scripts/atlas/**` (builder, fixtures, test), `wiki/test/**`, `wiki/e2e/**` | release-neutral | on merge |
| `wiki/src/atlas.ts`, `atlas-render.ts`, `libraries.ts`, `env.ts`, `index.ts`, `pages.ts` | release-affecting | only in a new release (fresh P1B, Jack's approval) |
| `site/assets/atlas.{css,js}`, `wiki/scripts/build-public.ts` | release-affecting | with that release's public baseline |
| `wiki/migrations/0015_atlas_feedback.sql` | operator action | applied to D1 after a backup; fail-soft until then |

- `atlas.css` and `atlas.js` are copied by build-public but are not in the required
  baseline lists, so baselines frozen before the atlas stay valid. The next baseline
  freeze includes them, and the source attestation is regenerated at release time as
  usual.
- build-public still deletes a stale `wiki/public/atlas.html`, and baselines still forbid
  one. That matters now: assets are served before the Worker, and a leftover old
  `atlas.html` would shadow the new `/atlas`.
- A release built from this branch with `ATLAS` unbound changes nothing a reader sees,
  except that `/atlas` answers a 503 page instead of the 404 page.

## 12. Later: the D1 import (proposal §11.2, gated)

Not built. It begins only once native Literature Links storage is live (migration 0014
applied and the cutover done). The importer would live in a release-neutral directory and:

- read each article with the bearer GET, merge deterministically, and POST with
  `base_version`, rebasing once on 409 and pacing itself under the write limiter;
- write per-article receipts keyed by the record set's sha;
- own identity: WikiLean item ids carry a passage lineage matched by span overlap on the
  text layer, and link ids are deterministic;
- supersede older-universe locate links on AI items only; never set legacy items
  `rejected`; send findings for human-owned items as proposals; and drop a passage that
  overlaps a human tombstone (`findLostHuman` stays the floor);
- pass an acceptance rehearsal on a copied D1: zero 400/413/422 and an idempotent second
  import.

## 13. The Brain path (proposal §11.3)

The atlas never writes the Brain. Locate output reaches it only (a) as
`brain/proposals/*.jsonl` through the offline fold, or (b) after Jack chooses a release-gate
mode for content changes. Concept edges come only from `subject` passages whose own QID was
validated against a sealed Wikidata bundle, and each library maps to one `brain_lib` token
per repository. Non-Mathlib libraries stay mentions-only by current policy.

## 14. Prerequisites and open decisions for Jack

Each decision below comes with a recommendation.

1. **Ratify the End-goal amendment** (`docs/ROADMAP.md`, "Proposed amendment"). Everything
   here assumes it.
2. **Store backend.** *Recommendation: R2.* Data updates without a Worker release, there is
   no per-version asset-count limit, and the free tier is ample. Its cost: R2 must be
   enabled on the account (wikipedia.ts records that it was not). The alternatives:
   - KV needs no account change, but on the free plan its 1,000 writes a day cannot take a
     ~40k-object upload (check the plan).
   - Static assets couple every data update to the ~5.5 h release chain and run into the
     asset-count limit (about 20,000 files on the free plan) at full scale.
3. **Who uploads, and from where.** *Recommendation:* the operator Mac, with a scoped R2
   token (Object Read & Write on the one bucket), never the fleet host and never the
   nightly. The upload script is not built; it would follow section 6.
4. **Public licence statement.** The page footer states the mapping as CC0, like WikiLean's
   annotations, and the quote fields as CC BY-SA 4.0. Proposal §10 requires that wording
   for a CC0 consumer. Confirm it before launch.
5. **Feedback export carries notes.** It is the one free-text field, unlike the research
   export. *Recommendation:* keep it, because reasons make reports usable, and the form
   says maintainers read notes.
6. **Absence feedback is not built.** Readers cannot yet say "this is formalized, here"
   against a "not found". That is the safety metric's most valuable label
   (false-not-formalized, §8.2). It needs a passage-level key, for example
   `(record id, universe sha)`. *Recommendation:* build it next, as a second verdict
   target.
7. **The producer contract notes** (section 3) need formal-companion's agreement before the
   first real set.
8. **Navigation.** `/atlas` is linked from atlas pages and from the article box, but not yet
   from the other shells' navigation. Adding it there changes cached bytes
   (`page:home`, `page:articles` and the render cache keys). *Recommendation:* add it in the
   release that binds the store.

## 15. Tests

- `scripts/atlas/test_build_store.py` (in `scripts/ci-python.sh`) checks fixture
  reproducibility, schema validity, the golden store, determinism, the manifest, the view
  model, title shards, 12 refusal cases, a YAML universe and the CLI.
- `wiki/test/atlas.test.ts` (in `npm run test:ci`) checks 503 when unbound, the APIs and
  pages on the fixture store, 404s, escaping, feedback auth, origin, validation, size cap,
  rate limit and missing table, the export, the article box with its cache isolation, the
  readers, and the library registry.
- `wiki/test/retired-routes.test.ts` now excludes `/atlas` from the deleted paths, but still
  checks that `atlas` is reserved.
- `wiki/e2e/atlas.spec.ts` (the non-required browser soak) runs axe in both colour schemes,
  plus the filters, feedback, live search and the article box.
