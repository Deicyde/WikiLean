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
| `EDIT_LIMITER` on every write, 30/min | Feedback writes take `EDIT_LIMITER`; a dashboard search takes the per-IP anonymous read budget (section 7); other reads are not limited, and index shards are cached per isolate |
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
`id`, `repo` (an https repository URL), `commit` (40 hex), and optionally `display_name`,
`brain_lib`, `toolchain`, `roots`, `licence: {spdx}` and `src_dir`), and `excluded` (a
list, each `{id, reason, repo?}`), as in proposal §2.3. `display_name` and `src_dir` are
WikiLean additions (contract note 6).

`sources.jsonl`: `source_id` (`enwiki-<pageid>-<revision>`), `pageid`, `revision`, `title`,
`wikidata`, `html_sha256`, `layer_sha256`, `renderer` (a short token such as `web-html/1`,
or null), and `assessment: {class, importance}` (the WikiProject assessment, or null).

### What the builder checks

`scripts/atlas/build_store.py` refuses the whole set, listing every problem it found, when
any of these fails:

1. **Seal.** `SEAL.json` has the fields above; every listed file exists and matches its
   sha256; no unlisted file, symlink or unsafe path is present. Receipts are named by
   their own sha256, and every receipt is referenced.
2. **Universe.** Its id and `universe_sha256` equal the seal's; library ids and
   repositories are unique; each repository is an https URL with a dotted host and one to
   four path segments, none of them `.` or `..`; commits are 40-hex. A
   `blob_url_template` is refused (contract note 6), and so is a `display_name` that is not
   at most 100 printable ASCII letters, digits, spaces and `,.()/+:'&_-` or `·` (no
   zero-width, soft-hyphen or look-alike character). `excluded` is a list. A
   `universe.yaml` that does not parse, or repeats a key, is refused like duplicate-key JSON.
3. **Sources.** `source_id` agrees with `pageid` and `revision`; there is one revision per
   page per set, and one page per title (two titles with the same `title_key`, such as
   `Widget lemma` and `Widget_lemma`, are refused, because the index and the article box
   find a page by its title key); every source has a records file, and every records file
   has a source; `renderer` is a short token or null.
4. **Schema.** Every record passes the vendored Literature Links schema and its numeric
   checks, through the checker WikiLean already uses: `site/native_annotations.validate_link_record`
   on `wiki/src/vendor/literature-links/companion.schema.json`, whose sha256 is checked
   against `provenance.json` first. JSON with duplicate keys, NaN, a number too large for
   a double (`1e400`, which parses as infinity) or nesting too deep to parse is refused.
5. **Records (proposal §2.7).** Each record carries the `org.mathlib-initiative.formal-companion.locate`
   extension with `item` (`In-text #<8-16 hex>`), the set's universe id and sha, and a
   `challenge` value; `id` is `<source_id>::<item>`. The subject is one `html` end at the
   source's revision: its `uri` is an `https://en.wikipedia.org/w/index.php` (or `/wiki/`)
   link whose parsed query has exactly one `oldid`, equal to the revision (so `oldid=20029`
   does not pass for revision 2002), and its `version` is the revision. Its `pageid` and
   `html_sha256` match `sources.jsonl`, and its quote is within the export caps (12 words
   of `exact`, 32 characters of prefix and of suffix, all strings).
   - A **location** (relation `exactMatch`, `closeMatch`, `broadMatch`, `narrowMatch`,
     `hasPart` or `relatedMatch`) has one `lean` end at its library's repo and commit, with
     `declaration`, `module`, a safe `.lean` `path`, `1 <= line <= end_line`, a
     `proof_status`, a unique 64-hex `location_key`, `rank` 1-5 and a `confidence`. A
     `closeMatch` names its convention difference in `note`. The lean end's locate
     extension may carry `statement`, the library index's flag for a Prop-valued
     definition (contract note 8): it must be `true` or `false` (absent or null is false),
     and is refused on a `proved` declaration, as the producer refuses it. Every location
     record carries its passage's derived status, except the `relatedMatch` records of a
     passage that has none (check 6).
   - An **absence** (`noMatch`) has status `not_formalized` and exactly one whole-library
     `lean` end per universe library, and references its receipt as
     `receipt: {sha256, queries, candidates}`. The counts must match the receipt file, and
     the receipt must list at least 3 distinct queries (gate rule 5).
   - An **unresolved** passage is a single record with `object: []`, no status and no
     relation.
6. **Passages (proposal §2.6).** The records of one passage agree on kind, quote and status.
   A passage has at most one absence and at most 5 locations, with distinct ranks. An
   absence sits only beside `relatedMatch` locations, and `not_formalized` needs one.
   **The builder recomputes the derived status** of every located passage from its
   kind and its locations' relations, proof statuses and `statement` flags
   (`derive_status`), and refuses the set when the stamped status differs, in either
   direction: a sorry-backed match stamped `formalized` is refused, and so is a proved
   exact match stamped `statement_only` or `partial`. `derive_status` is a port of the
   producer's own `cflib.vocab.derive_status` (formal-companion `scripts/cflib/vocab.py`,
   which implements the decisions `references/vocabulary.md` §1.11 makes for the cases the
   proposal's table leaves open), so it accepts exactly the one status the producer
   stamps. Each location that counts gets a status from its passage kind's row; the
   passage takes the best of them (formalized, then statement only, then partial), and a
   `relatedMatch` never counts:

   | location | theorem, conjecture | definition, construction, notation |
   |---|---|---|
   | `hasPart` or `narrowMatch`, to anything | partial | partial |
   | `broadMatch`, to anything | as an exact match | partial |
   | exact or close match to a `proved` declaration | formalized | partial |
   | exact or close match to a `statement_only`, `wanted` or `axiom_dependent` declaration | statement only | statement only |
   | exact or close match to a `definition` with the `statement` flag (Prop-valued) | statement only | formalized |
   | exact or close match to any other `definition` (data) | partial | formalized |

   An `example` or `other` passage is judged by both columns and takes the better answer.
   A located passage whose only locations are `relatedMatch`, with no checked absence, has
   no status (the table's "(none)" row): its records carry none, and the builder stores it
   as outcome `unresolved`, counted as undecided (never as not formalized), with its
   related declarations kept. Such a passage whose records stamp a status is refused. The
   location that justifies the status, the highest-ranked one whose own status is the
   passage's, is stored as the passage's `basis`, so the page names it rather than
   whichever location ranks first. `scripts/atlas/fixtures/producer_derive_status.json`
   holds the producer's answers for every one-location case and 300 sampled two- and
   three-location cases, generated by `make_producer_table.py` from formal-companion at
   `b4ac86a` (branch `claude/wikiproject-math`, also `locate/foundation`; not yet on its
   master), and both the builder and the page's fallback (`locationStatus`) are tested
   against it.

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
   revisions of one page is refused. For the same reason, two pages with one title key are
   refused.
6. **Library naming and source links come from WikiLean, not from the set.** A library's
   `display_name` is shown only for a repository the registry does not seed
   (`wiki/src/libraries.ts`), only when it is plain printable ASCII (the builder refuses
   any other), and never when its skeleton (letters and digits, lowercased, with `I`, `i`
   and `1` read as `l`, `0` as `o`, `rn` as `m`) contains a seeded library's, so a
   universe cannot relabel a repository as Mathlib, whether as `Mathlib`, `Mathlib\u200b`,
   `Mаthlib` (Cyrillic а), `MathIib` or `Mathlib 4`. Any other name falls back to
   owner/repo. `blob_url_template` is not part of the contract: every "Source @sha7" link
   is built from GitHub's blob scheme for the library's own `github.com/<owner>/<repo>`,
   and a non-GitHub repository gets no source link, and no repository link on the
   universe page either (it is named there as text). A set carrying a template is
   refused, so a producer learns it is unsupported. `src_dir` (the directory a record's
   `path` is relative to) stays, and must be a safe relative path.
7. **WikiLean recomputes the derived status** (section 3, check 6) with the producer's
   rules, as `cflib.vocab.derive_status` implements them at formal-companion `b4ac86a`.
   That code is not yet on formal-companion's master, and `references/vocabulary.md` §1.11
   marks its decisions as conservative, "until the maintainer says otherwise": a
   construction or notation passage is a definition passage, an example or `other`
   passage takes the better of the two rows, a theorem passage matched exactly or closely
   to data is partial (an instance such as `Real.instField` stating "ℝ is a field" reads
   partial, which §1.11 lists as an open question), and a definition passage matched to a
   proved theorem is partial. When the producer changes a rule, regenerate
   `producer_derive_status.json` and port the change; the parity test fails until both
   agree. A located passage the table gives no status is unresolved (note 9), never
   guessed.
8. **The `statement` flag rides in the lean end's locate extension.** Proposal §2.4 puts
   it in the library index beside `proof_status`, and §2.7 joins `proof_status` into the
   lean end at export but does not list `statement`. The builder reads it from the same
   place (`object[0].extensions.‹locate›.statement`). Without it, a theorem passage
   matched to a Prop-valued definition, which the producer stamps `statement_only`,
   recomputes as `partial` and the set is refused, so the export must carry it.
9. **A passage with only `relatedMatch` locations** (no checked absence) has no derived
   status, and the producer's export for it is not built yet. WikiLean reads it as
   `relatedMatch` location records without a `status`, which it stores as an unresolved
   passage that keeps its related declarations. An unresolved record (`object: []`)
   beside them is refused, as is a status on them.

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
  `subject_uri`, `outcome` (located, absent or unresolved), `status`, `basis` (the
  `location_key` of the location that justifies the status, or null), and `locations`,
  each with `library, repo, commit, declaration, module, path, line, end_line, relation,
  proof_status, statement, rank, confidence, challenge, location_key, note` (`statement`
  is the Prop-valued-definition flag, false when the record omits it). A passage also
  carries `absence {universe, universe_sha256, libraries, receipt, challenge}` or
  `unresolved {challenge}`; for a passage that is unresolved because its only locations
  are `relatedMatch`, `challenge` is its rank-1 location's and the locations are kept. A
  `relatedMatch` states something else (proposal §2.6), so it is counted
  in `related` and never as a location. `summary.by_library` (and `current.json`'s
  `counts.by_library`) splits each library's `locations` into `complete` (proved
  declarations and definitions) and `incomplete` (`statement_only`, `wanted`,
  `axiom_dependent`), so a library that only states results is never counted as one
  that proves them.
- **Index shards.** The key is the first hex digit of `sha256(title_key)`, which gives 16
  shards. `title_key` is the NFC title with underscores and Unicode spaces collapsed to
  one space and trimmed, the same in Python (`title_key`) and TypeScript (`titleKey`). A
  test in each language pins the same shards. Rows are arrays named by the shard's
  `fields` list, so a field can be appended within v1; the last one, `libraries`, lists
  the ids of the libraries that hold a location for the article, in universe order. A
  shard over 1 MiB is refused; a wider key would be a v2 store.
- **Determinism.** Output is canonical JSON (sorted keys, compact, UTF-8, newline) in a
  stable order: passages in record order, locations by rank, index rows by title.
  `built_at` defaults to SEAL's `created`, so the same set always builds the same bytes.

## 5. Building

```bash
python3 scripts/atlas/build_store.py --record-set <set> --out <store>            # new directory only
python3 scripts/atlas/build_store.py --record-set <set> --out <store> --check    # compare, write nothing
python3 scripts/atlas/build_store.py --record-set <set> --out <store> --replace  # rebuild in place
```

Exit codes: 0 is success, 1 means `--check` found differences, 2 means the record set
was refused (the reasons go to stderr), and 3 means the builder itself failed (a bug;
nothing was written). Every check runs before any view is computed, and a value of the
wrong type (a list where a vocabulary word belongs, a number where `excluded` lists
belong, `1e400`) is reported and dropped rather than used, so a malformed set is always a 2,
never a crash that could be read as "`--check` found differences". A test sets nine
hostile values (null, a bool, numbers, a string, lists, objects) into some 60 fields of
the fixture in turn and requires a refusal or a build, never another exception. The tree
is staged next to `--out` and moved into place, and the previous store is moved back if
that fails, so a failed build leaves nothing half-written. `--out` is the store directory
itself: `--replace` refuses a directory that holds anything besides `atlas/v1/`, so
pointing it one level too high (at `wiki/public`, say) cannot delete unrelated files. `--built-at <ISO-Z>` overrides the
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
   `index/`, `manifest.json`), keyed by its path, overwriting what is there. That
   includes an existing `universes/<sha>.json`: the key is the producer's
   `universe_sha256`, which covers the libraries' ids, commits and index shas but not the
   display metadata beside them (`src_dir`, `display_name`, exclusions), so the object is
   not content-addressed and a later campaign on the same frozen universe may change it.
   Each article object is self-contained, naming its own campaign, universe and record
   set.
3. Upload `current.json` last, then delete objects the new manifest no longer lists.

While an upload is running, readers can see a newer article next to an older index. Each
article stays internally consistent, so the worst case is a short-lived 404 from the index
for a newly added page. No upload script exists yet, deliberately: it is a remote write,
and who runs it is an open decision (section 14).

## 7. Worker routes

| Route | Answer | Cache-Control |
|---|---|---|
| `GET /atlas[?q=]` | dashboard: totals by status, by library (complete and incomplete locations), by WikiProject importance; article list with the libraries holding each article, searched server-side through the index shards (200 rows shown). A `q` search takes the anonymous read budget of `/api/brain/*` and `/mcp` (per IP, `MCP_LIMITER` else `BRAIN_API_LIMITER`, 120/min, key `atlas-q:<ip>`) and answers 429 over it | none (HTML, like other pages) |
| `GET /atlas/:pageid[/:slug]` | one article's coverage page; the slug is cosmetic | none |
| `GET /atlas/universe/:sha` | the universe's libraries, commits and exclusions | none |
| `GET /api/atlas/current` | `current.json` | `public, max-age=60` |
| `GET /api/atlas/article/:pageid` | the view model | `public, max-age=300` |
| `GET /api/atlas/records/:pageid` | the raw records, `application/x-ndjson` | `public, max-age=300` |
| `GET /api/atlas/universe/:sha` | the universe | `public, max-age=300` (not immutable: see section 6) |
| `GET /api/atlas/index/:shard` | one index shard (used by the dashboard's live filter) | `public, max-age=300` |
| `POST /api/atlas/feedback` | section 10 | none |
| `GET /api/atlas/feedback/export` | section 10 | `no-store` |

With `ATLAS` unbound, every route above except the export answers **503 "atlas store not
configured"** (an HTML page or JSON, with `no-store`). A bound but empty store answers 503
"atlas store has no published build". An unknown or malformed page id, sha or shard
answers 404. `atlas` was already in `RESERVED`, so no article can take the name. The
security middleware (`src/security.ts`) adds its headers to every one of these responses.

Parsed index shards are cached per isolate for 60 seconds (`INDEX_CACHE_MS` in
`src/atlas.ts`), so the dashboard and the article box do not re-read and re-parse them on
every view. A newly published store reaches them within a minute, the staleness
`/api/atlas/current` already allows.

## 8. UI

The UI is server-rendered (`wiki/src/atlas-render.ts`) with one small vanilla enhancement
(`site/assets/atlas.js`) and one stylesheet (`site/assets/atlas.css`), both copied by
`wiki/scripts/build-public.ts`. The pages work without JavaScript, except Confirm/Report
and the live list filter.

- **Coverage strip.** Under a "Coverage" heading: passages checked and Lean locations, a
  coverage bar whose segments are filled with patterns (solid, hatched, dotted,
  cross-hatched, grey vertical hatch for undecided), and filter chips by status and by
  library. Each status chip carries a swatch of its fill, so the chips are the bar's
  legend. Status and library are two facets combined with AND ("Showing 0 of 4 passages:
  Statement only, in Mathlib."); pressing a pressed chip clears its own facet. A chip with
  a zero count is shown flat and inert, like the dashboard's totals legend. A library with
  only related declarations is labelled "related only".
- **Passage block.** Under a "Passages" heading, one `<h3>` per passage with its kind,
  prominence and section path, beside a status pill (icon and word). A located passage
  then says in visible text which location gives it its status, for example
  "Formalized: Mathlib states this exactly (proved).", naming the store's `basis`, not
  simply the rank-1 location. Then the quote with its context, and a link to the passage
  in the pinned Wikipedia revision (a text fragment). The producer's item id
  (`In-text #<hex>`) is a `data-item` attribute, not reader-facing text. Passage
  permalinks (`#p-<hex>`) clear the sticky header (`scroll-margin-top`).
- **Location card.** One card per location: library chip, `@<sha7>`, the relation in
  words (exact match, close match, more general, special case, states part, related), and
  a proof-status badge. `statement only`, `wanted` and `axiom-dependent` are drawn as
  amber dashed alerts (the statement-only colour, not the not-formalized red) and are
  never hidden. Then the declaration (namespace faint, leaf bold,
  wrapping at `.` and `_`), its module and lines, a convention note for close matches,
  the provenance line (rank, confidence, challenge), and "Source @<sha7>" linking
  `<repo>/blob/<commit>/<path>#L<line>-L<end_line>`.
- **Absence:** "Searched N libraries in universe <id> (<frozen>) — not found", linking the
  universe page, with the receipt's counts. **Unresolved:** "Undecided", never counted as
  not formalized; a passage unresolved because only related declarations were found shows
  them under "Related, but states something else". A Prop-valued definition (the
  `statement` flag) is badged "Prop definition" and described as stating the claim
  without proving it.
- **Dashboard.** The by-library table splits locations into complete (proved or a
  definition) and incomplete (statement only, wanted, axiom-dependent), so a library that
  only states results never reads like one that proves them. The article list has a
  Libraries column (the index rows' `libraries`, as short names).
- **Libraries** (`wiki/src/libraries.ts`, contract note 6). Names come from the seeds
  (Mathlib, Lean core with `src/`, Batteries, TauCeti, Formal Conjectures, atlas-lean),
  then the FormalFrontier organisation pattern ("FormalFrontier · <repo>"), then
  owner/repo for any other GitHub repository. A universe entry's `display_name` is used
  only for a repository the seeds do not name, only when it is printable ASCII, and never
  when its skeleton contains a seeded library's name (contract note 6). A universe's
  `blob_url_template` is ignored (the builder refuses it): every source link is GitHub's
  blob scheme for the library's own repository, a non-GitHub repository gets none, and
  owner or repository names `.` and `..` get none. The universe page links a repository
  only when it is `github.com/<owner>/<repo>` and names any other as text. The universe's
  `src_dir` is kept.
- **Accessibility.** Status is never shown by colour alone: each status has an icon, a word
  and its own stroke (solid, dashed, dotted, double, grey groove). Chips are real
  buttons with `aria-pressed`, and filter results are announced through a status region.
  The layout has 16px gutters on phones and no horizontal page scroll; a wide table
  scrolls sideways inside a labelled, focusable region ("Table: …"), with its row labels
  sticky below 720px. Only `/atlas` itself marks the Atlas nav link `aria-current="page"`.
  Colours are the stylesheet's tokens, so dark mode follows the site toggle and the OS
  preference. The browser tests (`wiki/e2e/atlas.spec.ts`) run axe on the coverage page
  and dashboard in light and dark mode, at desktop width and at 375px (with the universe
  page, checking that the page never scrolls sideways), and on an article page with the
  box. They also drive the filters, the feedback flow, the live search and a passage
  permalink.
- **Security.** All store text is escaped (`htmlEscape`), and every store number goes
  through `int()`, so a string where a count belongs renders as 0, never as markup. A
  store list that is not an array (a universe's `libraries` or `excluded`) is read as
  empty, so its `length` never reaches the HTML and a missing list never makes a page a
  500. Links are built from validated
  parts or parsed and required to be https, and paths with `..`, absolute or empty
  segments get no link. The pages have no inline event handlers, and `atlas.js` writes
  only through `textContent` and `createElement`. The pages keep two inline scripts
  byte-identical to the other shells (the theme bootstrap and the header search), and the
  coverage bar uses `style="width:…"`. Both are allowed by today's Report-Only CSP, and a
  strict CSP would need hashes for the scripts and `style-src-attr` for the bar.

## 9. Article pages

When `ATLAS` is bound and the article's Wikipedia title has an index entry, the article
page shows a compact box above the body: passages checked, counts by status, and "See
where →" linking `/atlas/<pageid>/<slug>`. The page's header badges count WikiLean's own
annotations, so the box says its counts are separate from them ("Separately from this
page's annotations, the atlas checked N passages of Wikipedia revision R…"), and when the
atlas checked another revision than the page shows it adds "This page shows revision
R′.". `serveArticle` adds it **per request**, like
the staleness banner (`pages.ts injectAtlasSummary`), and never bakes it into
`render:v18`. The atlas changes independently of `articles.version`, so a cached box
would be stale for up to 30 days. The cached base bytes do not change, so **no render-cache
key bump is needed**, and the legacy annotation rendering is untouched.

The box costs at most one R2 read (one index shard, cached for a minute per isolate) per
article view when bound, and nothing when unbound. A store failure is logged and drops the box; it never breaks the page. The match
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
`sha256("atlas-feedback\0" + user_id + PIPELINE_TOKEN)[:12]`. The domain prefix keeps
these pseudonyms apart from the research export's `sha256(user_id + PIPELINE_TOKEN)[:12]`:
this export carries notes, so a note that names its author must not let anyone join it to
that person's pseudonymous annotation history in the research export. Within this export a
person's pseudonym is stable, so their verdicts still group. The note is the only free
text. The page tells signed-in readers that maintainers read
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
   export, and its pseudonyms are domain-separated from that export's (section 10) so the
   two cannot be joined. *Recommendation:* keep the notes, because reasons make reports
   usable, and the form says maintainers read notes.
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
  model (including each passage's basis and the per-library proof split), title shards,
  the §2.6 recomputation in both directions, its parity with the producer's truth table
  (`producer_derive_status.json`) and the §1.11 cases, the `statement` flag, related-only
  passages, the other refusal cases (oldid, title keys, templates, dot-segment repos,
  display names, renderer, malformed or duplicate-key YAML, unknown statuses, non-string
  affixes), hostile values in some 60 fields (refused, never a crash), a YAML universe, and
  the CLI (exit codes 2 and 3, and `--replace` refusing foreign content).
- `wiki/test/atlas.test.ts` (in `npm run test:ci`) checks 503 when unbound, the APIs and
  pages on the fixture store, 404s, escaping and integer-only numbers, the headings, the
  visible status sentence and its basis, the shard cache and the search limiter, feedback
  auth, origin, validation, size cap, rate limit and missing table, the export and its
  domain-separated pseudonyms, the article box with its cache isolation and revision
  note, the readers, non-array universe lists, related-only and Prop-valued-definition
  passages, the producer's truth table for the page's fallback rows, and the library
  registry (seeded names, look-alike display names, ignored templates, dot segments,
  GitHub-only repository links).
- `wiki/test/retired-routes.test.ts` now excludes `/atlas` from the deleted paths, but still
  checks that `atlas` is reserved.
- `wiki/e2e/atlas.spec.ts` (the required browser job since PR #72) runs axe in both colour
  schemes at desktop width and at 375px, plus the combined filters, feedback, live search,
  a passage permalink and the article box.
