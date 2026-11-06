# The formalization reader: WikiLean's articles plane

**Status (2026-11-06): live, and from release R1 it takes community edits.** The articles
release was promoted on 2026-11-05 and batch 1's store went up the same evening, into the
private bucket `wikilean-articles`, under campaign `tierA-b1`; migration 0015
(`atlas_feedback`) was applied on 2026-11-02. The pre-upload audit ran on 2026-11-02/03 and
Jack approved the upload on its result (section 14, item 10).

Release **R1 of the public-editing plan** adds the plane's second write: **community tags**
(section 10a), with migration 0019. The Worker code, assets and migrations are
release-affecting and ship only in a release (section 11). The decisions Jack has made are in
section 14; the plan's own decisions 1–5, 11 and 13 are signed and are what section 10a
implements.

The articles plane answers one question per passage of a WikiProject Mathematics article:
**is this formalized in Lean, in which library, at which commit, and how closely?** The
answers come from the formal-companion skill's locate pipeline
(`docs/proposals/wikipedia-locate.md` in that repository, "the proposal" below), which
exports sealed sets of Literature Links 2.0 records. WikiLean receives them as a separate,
read-only data plane, as proposal §11.2 asks.

## 0. The rename (2026-11-05)

This plane was built as "the formalization atlas" and served `/atlas`. Jack decided on
2026-11-05 that it replaces WikiLean's old article system and takes over `/articles`, and
that the "atlas" name is dropped: facebookresearch's Lean library `atlas-lean` appears in
the plane's own library universe as "Atlas", so a page could show both names at once.
Release 1, this one, is the rename and the move. Deleting the old article system is a later
release: its pages at `/:slug` are still served and still readable, and `home.ts homePage()`,
the directory that served `/articles` until now, is kept in the tree but no longer routed.

**Changed by release R1 (2026-11-06).** Those pages no longer take edits. Decision 5, option
(b) of the signed public-editing plan carries Jack's 2026-11-05 retirement decision —
"Editing goes; Confirm and Report replace it" — so a human save, a proposal decision and an
agent annotation save at `/:slug` answer 403 and point at the article's `/articles` page
where a record set covers it. The pipeline bearer still writes, and revert, patrol, flags,
watchlists and every read are untouched. It is one row,
`site_settings.slug_editing` (migration 0019), so an admin can put it back without a
release. **The Release 2 cutover — deleting the routes and the assets, keeping and backing
up the D1 tables, and redirecting the roughly 730 uncovered URLs to Wikipedia — is a
separate act and waits for release R2**, which gives the overlay history, diffs and revert.

What the rename moved, and what it did not:

| was | is |
|---|---|
| `/atlas` (the list) | `/articles`, in place of the D1 article directory |
| `/atlas/:pageid[/:slug]`, `/atlas/universe/:sha` | the same under `/articles` |
| `/api/atlas/<sub>` | `/api/articles/<sub>` |
| — | every old URL above answers **301** to its new path, query string kept |
| R2 binding `ATLAS`, bucket `wikilean-atlas` | `ARTICLES`, `wikilean-articles` |
| store prefix `atlas/v1/`, schema ids `wikilean.atlas.*` | `articles/v1/`, `wikilean.articles.*` |
| `wiki/src/atlas{,-render,-math}.ts`, `site/assets/atlas{,-reader}.{css,js}`, `scripts/atlas/`, `docs/ATLAS.md` | the same with `articles` |
| the D1 table `atlas_feedback` | **unchanged.** Migration 0015 is applied in production and renaming a table needs a migration; its indexes and the `atlas_feedback_latest` view keep their names too |
| `atlas-lean`, and the "ATLAS" its registry entry shows | **unchanged.** It is facebookresearch's library, and this rename exists so that nothing else is called that |
| `atlas` in `RESERVED` (`index.ts`) | **still reserved**, so no article can be created under the old name |
| the old graph stack's bare `/api/atlas` and `/atlas_data.json` | **unchanged**: a plain 404, as since 2026-08-04 |
| `/api/articles` (the header search box's index of WikiLean's own articles) | **unchanged**, and not noindexed: the plane's robots middleware skips that exact path |

Two consequences worth stating. `/articles` now asks not to be indexed, as every page of
this plane does, so `sitemapXml` no longer lists it (`page:sitemap:v5`). And `/articles`
answers 503 while `ARTICLES` is unbound or the store has no `current.json`, where the old
directory always answered from D1.

One name to keep apart: facebookresearch's ATLAS Lean library (`atlas-lean`). It appears
here only as a universe library, and the proposal's example universe excludes it because
its licence forbids ML evaluation use. The old graph-stack "atlas" (`/atlas_data.json`, the
bare `/api/atlas`, an older `wiki/src/atlas.ts`), retired 2026-07-10 and deleted
2026-08-04, is a third thing again; older documents such as `docs/BRAIN.md` describe it.

## 1. Why a separate plane

WikiLean's per-article D1 annotations are the human-moderated layer, and their invariants
are load-bearing (CLAUDE.md, "Hard invariants"). Locate output differs in kind: tens of
thousands of passages, several libraries per passage, commit pins, checked absences. It
also arrives in bulk from a pipeline that WikiLean does not run. Writing it into D1 now
would collide with every invariant at once. The plane avoids that:

| D1 annotation path | Articles plane |
|---|---|
| Writes `articles.annotations` and bumps `version` | Never writes annotations; its only D1 table is `atlas_feedback` |
| 256 KB cap per article (`MAX_ANNOTATIONS_BYTES`) and 500 items | No cap: one static object per article |
| `EDIT_LIMITER` on every write, 30/min | Feedback writes take `EDIT_LIMITER`; a dashboard search, an article page and its two JSON views take the per-IP anonymous read budget (section 7); other reads are not limited, and index shards and the WikiLean slug lookup are cached per isolate |
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
  sealed record set  ----(operator copies)---->  scripts/articles/build_store.py
  (SEAL.json + files)                                 | validates, then writes
                                                      v
                                                 store tree articles/v1/...
                                                      | operator uploads (section 6)
                                                      v
                                                 R2 bucket, bound as ARTICLES
                                                      | read-only
                                                      v
                       Worker: /articles, /articles/:pageid, /api/articles/*, article-page box
                                                      |
                       signed-in readers: Confirm / Report  -->  D1 atlas_feedback
                                                      |
                       pipeline bearer: GET /api/articles/feedback/export  -->  benchmark seeds
```

The operator copy is the only way a set reaches WikiLean today, and it assumes one person
runs every locate run. [INTAKE.md](INTAKE.md) designs the submission endpoint that would take
a sealed `/2` set from an authenticated contributor machine instead, and
`scripts/atlas/verify_submission.py` is its checks, built: the store builder's refusals are
intake's, so the two contracts cannot drift. Nothing of the endpoint exists.

## 3. Input contract: the sealed record set

The record-set format is the one formal-companion and WikiLean agreed: the proposal's §2.3
and the skill's `references/locate-documents.md` §6, made precise below. Both sides
implement exactly this. The builder accepts this shape and refuses any other, naming the
key or the rule it breaks (an extra key such as `runs`, a missing one, a dict where a list
belongs).

```
<set>/SEAL.json                      the seal (below); lists every other file with its sha256
<set>/universe.json                  the universe (below); there is no universe.yaml form
<set>/sources.jsonl                  one row per source (article revision)
<set>/records/<source-id>.jsonl      that source's Literature Links 2.0 records
<set>/receipts/<sha256>.json         search receipts, named by the sha256 of their bytes
```

`SEAL.json` has exactly these keys:

```json
{"schema": "cf-locate-recordset/1",
 "set_id": "<token>",
 "campaign": "<campaign id> | run:<run-id>[+<challenge-run-id>]",
 "universe": {"id": "u1", "universe_sha256": "<64 hex>"},
 "sourceset": {"id": "<id>", "manifest_sha256": "<64 hex>"},
 "skill_commit": "<40 hex>",
 "created": "2026-10-17T12:00:00Z",
 "byte_budget": 0,
 "files": {"<relative path>": "<sha256>", ...}}
```

- **A token** (`set_id`, `universe.id`, `sourceset.id`, and in `universe.json` each library
  and exclusion `id`) is a letter or digit, then letters, digits and `._:-`, at most 128
  characters in all (`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`). So `smoke+2`, `_draft` and
  `wpm+math` are refused. A sourceset id is chosen before any run, so the producer should
  check it when the sourceset is created.
- **`campaign` is always a non-empty token**: the campaign id (letters, digits and `._:-`,
  at most 128, not starting `run:`), or for a run outside any campaign
  `run:<run-id>` or `run:<run-id>+<challenge-run-id>` (run ids of letters, digits and
  `._-`, at most 64 each). Never null. The pages show a run token as "run L with challenge
  run C, outside any campaign", and feedback rows carry the token as their campaign.
- **`byte_budget`** is the per-source byte budget the export kept to: a source's records
  file plus the receipts its absences reference. It is 0, meaning none, by default for a
  sealed set. A consumer with a cap of its own (the later D1 import, section 12) asks the
  export for one. When it is not 0, the builder refuses a source over it, and
  `current.json` records `set_id` and `byte_budget` beside the seal's sha.

`universe.json` has exactly the keys `id`, `universe_sha256` (never `sha256`), `frozen`
(YYYY-MM-DD), `libraries` and `excluded`:

- `libraries` is a **list, never a dict**, sorted by `id` (each id once). Each library has
  exactly `id`, `brain_lib`, `repo`, `commit` (40 hex), `toolchain`, `roots` (a non-empty
  list of module roots), `licence: {spdx}`, and optionally `display_name` (absent or null
  when the producer has none; contract note 6). `index`, `conventions`, `src_dir`,
  `blob_url_template` and any other key are refused.
- **Libraries are keyed by `id`, and a repository may be several of them** (contract note
  13). Proposal §2.3 lets a universe split one repository into libraries, one root each:
  lean-core as `init` (roots `Init`) and `std` (roots `Std`), both at one lean4 commit. The
  rule is: **each library `id` once, and each `(repo, root)` pair in one library only**. So
  two libraries may share a repository, at one commit or at two, but not a root of it, and
  one library may not list a root twice. A repository and a root are compared as written.
  Nothing else about a shared repository is refused: a true duplicate is the same id, or
  the same root of the same repository.
- **`repo`** is `https://`, a dotted host (no port, no user), then one to four path
  segments of 1 to 100 letters, digits and `_.~-` each, none of them `.` or `..`, with no
  trailing slash, query or fragment:
  `^https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/[A-Za-z0-9_.~-]{1,100}){1,4}$`. So
  `https://github.com/<owner>/<repo>` and `https://gitlab.example.org/group/sub/lean/beta`
  pass, and `https://github.com/<owner>/<repo>/` and a five-segment path do not. The
  Worker builds names and links from it (contract note 6), which is why it is strict. A
  repository is pinned when a library joins a universe, so the producer should check it
  there.
- **`licence` is `{"spdx": <SPDX id>}`, never null.** A library goes into a sealed
  universe with its licence known: the universe page shows it, and licences decide
  exclusions (atlas-lean's forbids ML evaluation use).
- **Lengths:** `brain_lib`, `toolchain` and each root are at most 300 characters, and an
  exclusion's `reason` at most 1000 (u1's longest is 215). A longer one is refused with a
  message giving its length.
- `display_name`, when present and not null, follows the grammar in check 2, and one that
  does not is refused, so a producer with no name in that grammar writes null.
- `excluded` is a list (empty when nothing is excluded) of exactly `{id, reason}`: a token
  id and a non-empty reason.

`sources.jsonl`: `source_id` (`enwiki-<pageid>-<revision>`), `pageid`, `revision`, `title`,
`wikidata`, `html_sha256`, `layer_sha256`, `renderer` (a short token such as `web-html/1`,
or null), and `assessment: {class, importance}` (the WikiProject assessment, or null).

Records follow proposal §2.7, with these points of the agreement:

- **Every record carries `section` and `prominence`** for its passage, in the record's
  locate extension: `section` is the heading path (a list of heading strings, `[]` in the
  lead) and `prominence` is `subject`, `central` or `supporting`. The coverage page marks
  the article's subject passages by it.
- **The quote is capped, and may be the passage's head.** The export quotes at most the
  first 12 words of a passage as `exact`, with at most 32 characters of `prefix` and
  `suffix` (proposal §2.7 and §10): the verbatim-quotation cap that the licence class of an
  open source allows a record (the skill's `POLICY.md`, "quotation, not reproduction"). The
  passage's full text stays in the producer's workspace. So the quote of a longer passage
  stops mid-sentence, and on smoke 2's real set 322 of 331 quotes do. The subject end's
  locate extension carries `span_sha256`, the sha256 of the whole passage span as it stands
  on the layer, trailing whitespace included (the skill's `scripts/locate/records.py`,
  `cap_quote` and `passage_records`), which is how the builder tells a capped head from a
  whole passage (`quote_capped` and `passage_ends_in_suffix`, section 4). It is required, 64
  lowercase hex (the subject end's locate extension, below), and where the subject end also
  carries F5's `span`, the span's length gives the builder the same answer more directly. The
  records of one passage carry the same `span_sha256` and the same `span`, as they carry the
  same quote, and records that disagree on either are refused.
- **A location's lean end** carries `location.declaration` and `location.module`, and in
  its locate extension `library`, `path` (relative to the repository root: Lean core's
  files are `src/Init/...`), `line`, and `end_line` when the library index records one.
  All of these are required except `end_line`: a lean end without a module, path or line
  is refused, so the producer should refuse such a passage itself, by name, rather than
  export it. atlas-lean's published index records only `line`, so `end_line` is then
  **absent** (never null), and consumers treat it as optional: the view model omits it,
  the page says "line N", and the source link is `#L<line>`.
- **`challenge`** is one of `survived`, `downgraded`, `not_required`, `sampled_out` and
  `challenge_pending` (proposal §2.7, `cflib.vocab.CHALLENGE_VERDICTS`). The last three
  are records no challenge judged, and the pages read each as unchallenged.
  `challenge_pending` marks a record that a prominence overturn to `subject` or `central`
  put under a challenge rule after the challenge ran: a challenge is owed and none has
  judged it. The pages say "not challenged yet (a challenge is pending)".
- **An absence's receipt counts** are `receipt: {sha256, queries, candidates}`, where
  `queries` and `candidates` are the lengths of the receipt file's `queries` and
  `candidates` lists, a repeated entry included (contract note 3).
- **A `closeMatch` always carries a non-empty `note`**, including one a challenge
  downgrade produced: the challenger's witness sentence becomes the note when the worker
  wrote none. A note-less `closeMatch` is refused.
- **An absence keeps one lean end per repository the universe searched, at its commit**,
  each minimal: `{"type": "lean", "uri": <repo>, "version": <40 hex>}` and nothing else (no
  `role`, no `extensions`, no `location`). That is one end per distinct `(repo, commit)` of
  the universe's libraries, each once: libraries split from one repository at one commit
  (Init and Std) are one search target and one end, and one repository at two commits is
  two (formal-companion `124a587`, `records.absence_ends`, which places each end by the
  first library by id). A universe whose repositories are all distinct gives one end per
  library, as before. On smoke-2's real set the minimal form shrinks an absence from about
  12 KB to about 7 KB with 48 libraries.
- **A coverage claim** (decision D1, check 6) rides in the record's locate extension as
  `coverage: {"complete": true, "parts": [<location_key>, ...], "challenge": <challenge
  value>}`: the passage's `hasPart` locations that together state every clause. It is a
  passage field, so every record of the passage carries the same claim, or none does; a
  passage without a claim has no `coverage` key (never null). `parts` names at least two
  distinct `location_key`s, each of a `hasPart` location of the same passage. The claim
  carries no note: the locate document's one-sentence note stays in the workspace. In the
  challenge stage every claim is challenged whatever its passage's prominence, and a refuted
  claim is removed (its parts stay `hasPart`), so an exported claim is normally `survived`;
  any other challenge value is accepted and upgrades nothing. `challenge` is never null: a
  claim no challenge has judged yet (owed) upgrades nothing and is not exported, and the
  producer's `derive_status` refuses one too. Confirming a claim confirms its parts, so a
  survived claim's part that no challenge judged on its own reads `survived` (check 6).
- **The subject end's locate extension** carries `pageid` (an integer) and `html_sha256`,
  both the source's in `sources.jsonl`; `renderer`, the source's renderer token (a short
  token of that grammar when `sources.jsonl` has none);
  `licence`, the source's SPDX licence id (`CC-BY-SA-4.0` for Wikipedia), a non-empty string
  of at most 300 characters; and `span_sha256`, the sha256 of the passage's span on the
  layer (64 lowercase hex). It may carry, both or neither, the passage's whole **`span`**
  and the **`layer_sha256`** it indexes, in exactly the shape formal-companion's
  split-frontend commit `722b30f` ("F5") writes them: `span` is `[start, end]`, the
  passage's half-open offsets on the layer in code points (never its text), two integers
  with `0 <= start < end`, and `layer_sha256` is 64 lowercase hex, equal to the source's
  `layer_sha256` in `sources.jsonl`. A consumer holding that layer can highlight the whole
  passage rather than only its capped quote, and the builder reads the span's length to tell
  a capped quote from a whole passage (section 4, "The passage's span"). The quote is the head of its passage, so
  `exact` is never longer than the span, and when the span's text after `exact` is no longer
  than the quote's `suffix` (the text right after `exact`), `exact` and the suffix's first
  characters are the span's whole text, and `span_sha256` must be its sha256. Sets from
  before F5 carry neither field and build as before.

### The locate extension's keys

The locate extension (`extensions["org.mathlib-initiative.formal-companion.locate"]`) is the
one place the Literature Links format admits fields of its own, so the schema cannot catch
a new, misspelt or malformed key there. The builder holds each of its three places to
exactly these keys, and refuses any other, naming the key, its place and the record id
(`record <source_id>::<item>: unexpected key 'spann' in the subject end's locate
extension`). A required key that is missing is named by the check of its value (check 5).

| place | required | optional |
|---|---|---|
| the record, every shape | `item`, `universe`, `universe_sha256`, `challenge`, `section`, `prominence` | `coverage` |
| a location record (any locate relation), besides | `location_key`, `rank`, `confidence` | |
| an absence record (`noMatch`), besides | `receipt`: exactly `{sha256, queries, candidates}`, the counts integers | |
| an unresolved record (no relation), besides | (none) | |
| the subject end | `pageid`, `html_sha256`, `renderer`, `span_sha256`, `licence` | `span` with `layer_sha256` |
| a location's lean end | `library`, `path`, `line`, `proof_status` | `end_line`, `statement` |

So a location's `rank` on an absence, or a `receipt` on a location, is refused as a key of
the wrong shape. `coverage` is allowed on every shape here and refused on an absence or an
unresolved record by check 6 (its parts must be `hasPart` locations of its passage). The
`extensions` object of a record, of its subject end and of a location's lean end holds the
locate extension and nothing else: another namespace is refused by name, as an absence's
lean ends carry no `extensions` at all. Keys of the Literature Links format itself (an end's
`type`, `uri`, `version`, `role`, `location`, `citation`) are the vendored schema's to check
(check 4).

### The `/2` record set (status rules 2 and 3)

A locate run of status rule 2 or 3 exports `cf-locate-recordset/2` (formal-companion
`references/locate-documents.md` §6, "The record set of status rule 2"; the builder reads it
from formal-companion `d85385a`). It is `/1` with these differences, and the builder accepts
exactly them; a `/1` set builds byte for byte as before.

- `SEAL.json` carries `/1`'s keys and `status_rule`, an integer, 2 or 3: the derivation every
  status was derived by. `/2` without it, `/1` with it, rule 1 under `/2` and any other value
  are refused.
- Every record's locate extension carries `passage_key` = sha256(`<pageid>\n<span_sha256>`),
  and a location record's `pair_key` = sha256(`<passage_key>\n<lean end uri>\n<declaration>`);
  the builder recomputes both. A `pair_key` on an absence or an unresolved record, and either
  key under `/1`, is refused by name.
- A coverage claim is a **bundle**, exactly `{complete, parts, challenge, relation, conjuncts,
  bundle_key}`: `relation` the bundle's (its weakest part's: exactMatch, closeMatch or
  broadMatch); `conjuncts` 2 to 5 `{id, text, part}` in id order `c1`..`cN`, each `part` one of
  `parts`, every part stating at least one; `bundle_key` = sha256 of the passage key and the
  parts' pair keys, sorted, one per line, recomputed. Under `/1` the claim stays the three keys.
- Statuses: under rule 2 a survived bundle is one more location **of its own relation** (rule 1
  makes it one more exactMatch). Rule 3 (R-BROAD) is rule 2 plus two rows: a broadMatch to data
  formalizes a theorem passage, and a broadMatch to a `definition` formalizes a definition
  passage. The parity tables are formal-companion's `status-parity-2.json` and `-3.json`
  (`scripts/articles/fixtures/status_parity-{2,3}.json`, the producer's byte for byte but for
  their decision texts, shortened here; the tests pin both) and the producer's own answers per rule
  (`producer_derive_status-{2,3}.json`, `make_producer_table.py --status-rule`).
- A subject end may carry `identifiers: {wikidata: <the source's QID>}`, on a subject passage's
  records only (decision B5); anything else there is refused.

The view model stays `wikilean.articles.article/1`, with optional fields: each location's
`pair_key` (which the feedback route copies into its row), each passage's `passage_key`, the
bundle's `relation`, `conjuncts` and `bundle_key` in `coverage`, and the article's `seal`
(`set_id`, `schema`, `status_rule`, `created`, `skill_commit`). `current.json`'s `record_set`
gains `schema` and `status_rule`.

### What the builder checks

`scripts/articles/build_store.py` refuses the whole set, listing every problem it found, when
any of these fails:

1. **Seal.** `SEAL.json` has exactly the keys above, with the token grammar for `set_id`,
   `universe.id` and `sourceset.id`, the campaign token grammar and a `byte_budget` that
   is an integer 0 or more; every listed file exists and matches its
   sha256; no unlisted file, symlink or unsafe path is present. Receipts are named by
   their own sha256, and every receipt is referenced. Hex, token and time values must
   match in full (a trailing newline does not pass).
2. **Universe.** `universe.json` has exactly the keys above (a `universe.yaml` is refused);
   its id and `universe_sha256` equal the seal's; `libraries` is a list sorted by id, each
   id once, and each `(repo, root)` pair belongs to one library (a repository may be several
   libraries, above; a repeated id or a root of one repository in two libraries, or twice
   in one, is refused, naming the library that holds it); each repository follows the
   `repo` grammar above (an https URL
   with a dotted host and one to four path segments, none of them `.` or `..`, and no
   trailing slash); commits are 40-hex; `brain_lib` and `toolchain` are non-empty strings
   of at most 300 characters, `roots` a non-empty list of them, and `licence` exactly
   `{spdx}`, never null. A `blob_url_template` or `src_dir` is refused by name (contract note
   6), and so is a `display_name` that is not at most 100 printable ASCII letters, digits,
   spaces and `,.()/+:'&_-` or `·` (no zero-width, soft-hyphen or look-alike character).
   `excluded` is a list of `{id, reason}`: a token id and a non-empty reason of at most
   1000 characters. Each message names the rule it breaks (a reason that is too long says
   so, with its length).
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
   extension with `item` (`In-text #<8-16 hex>`), the set's universe id and sha, a
   `challenge` value (`survived`, `downgraded`, `not_required`, `sampled_out` or
   `challenge_pending`; the last three read as unchallenged), and its passage's `section`
   and `prominence`; `id` is
   `<source_id>::<item>`. The subject is one `html` end at the
   source's revision: its `uri` is an `https://en.wikipedia.org/w/index.php` (or `/wiki/`)
   link whose parsed query has exactly one `oldid`, equal to the revision (so `oldid=20029`
   does not pass for revision 2002), and its `version` is the revision. Its `pageid` (an
   integer, so `1001.0` does not pass) and `html_sha256` match `sources.jsonl`, its
   `renderer` is the source's (when it has one), its `licence` a non-empty SPDX id and its
   `span_sha256` 64 lowercase hex, and its quote is within the export caps (12 words of
   `exact`, 32 characters of prefix and of suffix, all strings). A `span` and
   `layer_sha256`, when present, come together, the span two integers `0 <= start < end` no
   shorter than `exact`, and `layer_sha256` the source's; where the quote gives the span's
   text in full, `span_sha256` is its sha256 (above). Every locate extension has exactly
   the keys of its place ("The locate extension's keys", above): an unknown key, or another
   extension namespace, is refused by name with its place and the record id.
   - A **location** (relation `exactMatch`, `closeMatch`, `broadMatch`, `narrowMatch`,
     `hasPart` or `relatedMatch`) has one `lean` end at its library's repo and commit, with
     `declaration`, `module`, a safe repository-relative `.lean` `path`, an integer
     `line` of at least 1 (all required; a missing or null one is named) and, when
     present, an integer `end_line` of at least `line`, a
     `proof_status`, a unique 64-hex `location_key`, `rank` 1-5 and a `confidence`. A
     `closeMatch` names its convention difference in a non-empty `note`. The lean end's locate
     extension may carry `statement`, the library index's flag for a Prop-valued
     definition (contract note 8): it must be `true` or `false` (absent or null is false),
     and is refused on a `proved` declaration, as the producer refuses it. Every location
     record carries its passage's derived status, except the `relatedMatch` records of a
     passage that has none (check 6).
   - An **absence** (`noMatch`) has status `not_formalized` and exactly one minimal
     `{type, uri, version}` lean end per distinct `(repo, commit)` of the universe's
     libraries, each once (an end per library of a split repository, the export before
     formal-companion `124a587`, names it twice and is refused), and references its receipt as
     exactly `receipt: {sha256, queries, candidates}`. Each count is an integer (`2.0` and
     `true` do not pass) equal to the length of that list in the receipt file, repeats
     included, and the receipt must list at least 3 distinct queries (gate rule 5).
   - An **unresolved** passage is a single record with `object: []`, no status and no
     relation.
   - A **coverage claim**, when present, has exactly `complete` (`true`), `parts` (a list of
     at least 2 distinct 64-hex location keys) and `challenge` (a challenge value above);
     `null`, a missing or extra key, `complete: false`, one part, a repeated part, a null
     `challenge` (an owed claim) or a value such as `refuted` is refused, naming the rule.
6. **Passages (proposal §2.6).** The records of one passage agree on kind, quote, section,
   prominence, coverage claim, status, and the subject end's `span` and `span_sha256`.
   A passage has at most one absence and at most 5 locations, with distinct ranks. An
   absence sits only beside `relatedMatch` locations, and `not_formalized` needs one. A
   coverage claim names only `hasPart` locations of its own passage (a key of another
   passage, of no location, or of a `narrowMatch` or other location is refused), so a claim
   on an absence or an unresolved passage is refused too.
   **The builder recomputes the derived status** of every located passage from its
   kind and its locations' relations, proof statuses and `statement` flags
   (`derive_status`), and refuses the set when the stamped status differs, in either
   direction: a sorry-backed match stamped `formalized` is refused, and so is a proved
   exact match stamped `statement_only` or `partial`. `derive_status` is a port of the
   producer's own `cflib.vocab.derive_status` (formal-companion `scripts/cflib/vocab.py`,
   which implements the decisions `references/vocabulary.md` §1.11 makes for the cases the
   proposal's table leaves open), so it accepts exactly the one status the producer
   stamps. Each location that counts gets a status from its passage kind's row, and a
   survived coverage claim gives one more (below); the passage takes the best of them
   (formalized, then statement only, then partial), and a `relatedMatch` never counts:

   | location | theorem, conjecture | definition, construction, notation |
   |---|---|---|
   | `hasPart` or `narrowMatch`, to anything | partial | partial |
   | `broadMatch` to data (a `definition` without the `statement` flag) | partial | partial |
   | `broadMatch` to anything else | as an exact match | partial |
   | exact or close match to a `proved` declaration | formalized | partial |
   | exact or close match to a `statement_only`, `wanted` or `axiom_dependent` declaration | statement only | statement only |
   | exact or close match to a `definition` with the `statement` flag (Prop-valued) | statement only | formalized |
   | exact or close match to any other `definition` (data) | **formalized** (D1) | formalized |

   **The status rules as the maintainer decided them (decision D1, 2026-10-18).**
   Verbatim: "D1: yes. Formalized when the hasPart locations together cover every clause
   (challenger confirms coverage), and when an exact/closeMatch is to an equivalence or
   data declaration that states the theorem (e.g. CRT's RingEquiv). Update vocab and
   WikiLean's parity table." It replaces two of `references/vocabulary.md` §1.11's
   conservative decisions: a theorem matched exactly or closely to data was partial, and a
   conjunction no single declaration states could not be formalized. Exactly:

   - **Data states a theorem.** A theorem or conjecture passage with an `exactMatch` or
     `closeMatch` to a data declaration (`proof_status` `definition` without the
     `statement` flag: an equivalence, an isomorphism, a bijection, a structure-valued
     construction, such as CRT's `RingEquiv`) is formalized (the bold cell). A Prop-valued
     definition (the flag set) still gives statement only, a `broadMatch` to data is still
     partial, and data that rests on `sorry` has `proof_status` `statement_only` and stays
     statement only.
   - **A survived coverage claim** counts, for `derive_status`, exactly as one more
     `exactMatch` location, a fixed stand-in rather than any one of its parts
     (`coverage_location`). A part is *finished* when it is `proved` or data (a
     `definition` without the `statement` flag), and *unfinished* when it is
     `statement_only`, `wanted` or `axiom_dependent`, or a Prop-valued definition (the flag
     set) whatever its `proof_status`. When every part is finished, the stand-in is a
     `proved` declaration on the theorem row and a definition (data) on the definition row,
     so the claim gives formalized on either row; when any part is unfinished, it is a
     `statement_only` declaration on both rows, so the claim gives statement only. An
     `example` or `other` passage judges it on both rows, the better winning, as it judges
     any location. This
     is the reading of D1 the lead set in parity round three (2026-10-18), derived from
     the decision, and it resolves the open questions Q1 to Q3 (note 11): an `example` or
     `other` passage's claim takes both rows (Q1); a data part counts as finished, as an
     exact match to data formalizes a theorem (Q2); and the definition row has the
     statement-only case an exact match to an unfinished declaration has (Q3). So the
     stand-in is not an exact match to any one part, the least finished one included: on
     the definition row a claim of `proved` parts formalizes, though an exact match to a
     `proved` theorem alone is partial, and a claim with a Prop-valued-definition part gives
     statement only, though an exact match to one alone formalizes a definition passage
     (the shared table's D04 beside O07).
   - **Confirming a claim confirms its parts.** In the view model, a part whose own
     `challenge` is `not_required` or `sampled_out` (no challenge judged it on its own)
     reads `survived` once its claim survived (`confirmed_parts`); the records are copied as
     they are, so a producer that stamps its parts `survived` gives the same view. A part
     another verdict judged keeps it (`downgraded`, `survived`), as does one still owed a
     challenge (`challenge_pending`). That includes a part a prominence overturn to
     `subject` or `central` in the claim's own run reopened: the overturn put it under a
     rule that requires its own verdict, and **the reopen wins** (the lead's decision on
     review four), so it stays `challenge_pending` while the claim stays survived and the
     status is unchanged (shared case P05). The claim stands in only for parts no rule
     required. A part refuted, or downgraded out of `hasPart`, still
     lapses the claim: a claim naming it is refused (check 5 above) until the producer
     drops it. The producer's challenge planner keeps a claim and its parts in one task.
   - A claim whose challenge is not `survived` (pending, unjudged, downgraded, or a refuted
     claim, which is not exported) upgrades nothing and confirms no part: its parts count
     only as `hasPart` locations, partial each. Precedence is unchanged: the best status
     over all the evidence wins, and an absence beside a location that counts is still
     refused.

   An `example` or `other` passage is judged by both columns and takes the better answer.
   A located passage whose only locations are `relatedMatch`, with no checked absence, has
   no status (the table's "(none)" row): its records carry none, and the builder stores it
   as outcome `unresolved` (counted under `unresolved` in the store's counts, never as not
   formalized; the pages show it with no status until someone reads it, section 8), with its
   related declarations kept. Such a passage whose records stamp a status is refused. The
   location that justifies the status, the highest-ranked one whose own status is the
   passage's, is stored as the passage's `basis`, so the page names it rather than
   whichever location ranks first. When only the coverage claim reaches the passage's
   status, the basis is null and the claim's own `basis` is true; on a tie the location
   is preferred. `scripts/articles/fixtures/producer_derive_status.json` (WikiLean's parity
   table) holds the producer's answers for every one-location case, 300 sampled two- and
   three-location cases, and 959 coverage cases (every kind with every pair of parts under
   a survived claim, the pairs of four representative parts, two finished and two
   unfinished, under each other challenge value, and 200 sampled claims with three parts
   or with other locations beside the parts). It is generated by `make_producer_table.py`
   from formal-companion at `4fc2aa0` (branch `locate/d1`, which implements D1 as parity
   round three reads it, with review four's reopen ordering, and is now on its master), and
   the builder is tested against every case. Regenerated at `4fc2aa0`, every answer is the
   one `3225e95` gave (only the commit stamp moved: the reopen ordering changes exported
   challenge values, never a derived status). Against the table from the pre-D1 producer
   (`b4ac86a`), exactly 13 answers changed, each a theorem or conjecture with an exact or
   close match to data. Against the
   round-two table (`c165c65`), 394 answers changed, every one a coverage case under a
   survived claim: 363 from partial to statement only (Q2 and Q3, and Q1 for `example` and
   `other`), 17 from formalized to statement only (a definition, construction or notation
   passage's claim with a Prop-valued-definition part), and 14 from partial to formalized
   (data parts on a theorem, conjecture, `example` or `other` passage).
   `scripts/articles/fixtures/status_parity.json` is the table both sides run: formal-companion's
   hand-written `tests/data/locate/status-parity.json` at `4fc2aa0`, copied byte for byte
   (its sha256 is pinned in the test), 118 cases whose expectations were written from the
   decision's spec and the round-three reading it records under `reading`, 22 of them
   refusals and 15 marked with the question (Q1 to Q3) they turn on. Five (P01 to P05)
   carry `expected_challenges`, the challenge each location's exported record carries, and
   WikiLean's test checks the view model's `confirmed_parts` against them; P05 is review
   four's reopen (two parts `challenge_pending` under a survived claim stay pending). The
   producer's test runs its `derive_status` over the cases; WikiLean's runs
   `derive_passage_status`,
   which takes the producer's arguments
   (`passage_kind, locations, absent, coverage`) and puts one passage through the builder's
   own checks (above, and check 5's claim shape) and then `derive_status`, so a case the
   builder would refuse in a record set is a refusal there too. The page's fallback
   (`locationStatus`) is tested against the cases of both tables without a claim, since the
   store already carries a claim's effect.

### Contract notes for formal-companion

These are where the builder departs from, or adds to, the proposal as written. They were
checked on 2026-11-02 against formal-companion's master, whose locate code is the one at
`d85385a`, the commit the `/2` section names (master since changed only the Literature Links
version its comments name). The record-set format itself (above: the seal, the universe's list
shape, the campaign token, the byte budget, optional `end_line`, `section` and `prominence`,
closeMatch notes and minimal absence ends) is agreed with the skill (note 10), and the
producer's export at master writes what notes 1, 2, 8, 9, 12 and 13 ask. The rest are
WikiLean's readings where the agreed format left a choice; they wait on the agreement of the
producer's maintainer before the first real set is published (section 14, item 7):

1. **CSL citations need an `id`.** The vendored schema requires `citation.id` and
   `citation.type`. Proposal §2.7's example citation has no `id` and fails validation as
   written. The fixture uses the source id, and so does the producer's export
   (`records.subject_end`).
2. **`section` and `prominence` ride in every record's locate extension** (agreed;
   required): `section` is the passage's heading path (`[]` in the lead), `prominence` is
   `subject|central|supporting`. §2.7's example record shows neither, and the sealed set
   has no passage table, so the records carry them. The coverage page shows both and marks
   the article's subject passages. The "Gadget (mathematics)" fixture is a lead passage
   (`section: []`).
3. **Absence receipts.** The proposal says "referenced by sha256 and count". The builder
   reads that as `receipt: {sha256, queries, candidates}`, where each count is the length
   of that list in the receipt file. A repeated query stays in the file and is counted, so
   a producer that writes the number of distinct queries while keeping the repeat is
   refused (the message gives both numbers). Gate rule 5's floor of 3 is on distinct
   queries.
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
   refused, so a producer learns it is unsupported. A lean end's `path` is relative to the
   repository root (Lean core's records say `src/Init/...`), so there is no `src_dir`
   either: a set carrying one is refused, and the registry prefixes nothing.
7. **WikiLean recomputes the derived status** (section 3, check 6) with the producer's
   rules, as `cflib.vocab.derive_status` implements them on formal-companion's master: status
   rule 1 as at `4fc2aa0`, with the maintainer's decision D1 (2026-10-18) as parity round three
   reads it (note 11), and status rules 2 and 3 as at `d85385a` (the `/2` section).
   `references/vocabulary.md` §1.11 there still marks its other
   decisions as conservative, "until the maintainer says otherwise": a construction or
   notation passage is a definition passage, an example or `other` passage takes the
   better of the two rows, and a definition passage matched to a proved theorem is
   partial. D1 replaced the decision that a theorem passage matched exactly or closely to
   data is partial (an equivalence such as CRT's `RingEquiv`, or an instance such as
   `Real.instField` stating "ℝ is a field", now formalizes it) and added coverage claims.
   When the producer changes a rule, regenerate
   `producer_derive_status.json`, copy its `tests/data/locate/status-parity.json` to
   `status_parity.json` (and its sha256 into the test), and port the change; the parity
   tests fail until both agree. A located passage the table gives no status is unresolved (note 9), never
   guessed.
8. **The `statement` flag rides in the lean end's locate extension.** Proposal §2.4 puts
   it in the library index beside `proof_status`, and §2.7 joins `proof_status` into the
   lean end at export but does not list `statement`. The builder reads it from the same
   place (`object[0].extensions.‹locate›.statement`). Without it, a theorem passage
   matched to a Prop-valued definition, which the producer stamps `statement_only`,
   recomputes as `partial` and the set is refused, so the export must carry it. It does
   (`records.lean_end` writes `statement: true` on such an end).
9. **A passage with only `relatedMatch` locations** (no checked absence) has no derived
   status. The producer's export writes it as exactly what WikiLean reads: its
   `relatedMatch` location records without a `status`, and no absence
   (`records.passage_records`), which the builder stores as an unresolved passage that keeps
   its related declarations, and which the pages set aside until it is read (section 8). An
   unresolved record (`object: []`) beside them is refused, as is a status on them.
10. **The agreed record-set format** (2026-10-18, after smoke 2, whose export WikiLean
    refused on seven mismatches). Both sides implement section 3 exactly. Where the
    agreement named a field but not its spelling, WikiLean chose: the budget is SEAL's
    `byte_budget` (the name the export's option and report already use), a campaign id may
    not start `run:`, run ids are letters, digits and `._-`, `display_name` is the one
    optional library key (absent or null), and `section` is `[]` in the lead. Where the
    agreement named a value but not its grammar, WikiLean's builder states it in section
    3: the token grammar of `set_id`, `universe.id` and `sourceset.id`, the `repo`
    grammar, the length caps (300 characters for `brain_lib`, `toolchain` and roots, 1000
    for an exclusion's reason), a `licence` that is never null, a `display_name` that is
    null unless it fits the display grammar, `module`, `path` and `line` required on every
    location's lean end, receipt counts as list lengths, and `challenge_pending` among the
    challenge values. The producer is asked to enforce each where the value is first
    chosen (`cf sourceset create`, `cf corpus add`, `cf plan-locate`), so a set is not
    refused only after every run has been paid for. The token grammar is enforced so at
    master (`cflib.vocab.RECORDSET_TOKEN`, in `cf sourceset create`, the campaign planner and
    the export). Any other key is refused, so a change to the format is a change on both
    sides at once.
11. **The coverage claim's export** (decision D1, 2026-10-18). The agreed spec puts the
    claim in "the passage's record extensions[locate].coverage" as `{complete, parts,
    challenge}`, `parts` as location keys, valid only with at least two parts, each a
    `hasPart` location of the passage. Where it left the spelling open, WikiLean chose, and
    asks the producer to confirm: every record of the passage carries the same claim (as
    each carries `section`, `prominence` and the status the claim justifies), or none does;
    a passage without a claim has no `coverage` key, never `null`; `challenge` takes the
    records' challenge vocabulary, so `refuted` is refused (a refuted claim is removed, not
    exported); and `parts` keeps the producer's order, which the page lists. In rounds one
    and two both sides read the decision literally where the maintainer had not said more,
    and the skill's `references/vocabulary.md` §1.11 named three readings as open
    questions: **Q1**, an `example` or `other` passage's claim judged by the theorem row
    only (D1 names the definition row for definition, construction and notation); **Q2**, a
    theorem row's part judged by its `proof_status` alone, so a data part or a Prop-valued
    definition whose `proof_status` is `definition` upgraded nothing; **Q3**, a definition
    row's claim with an unfinished part upgrading nothing (no statement-only case). Round
    three resolves all three (below).
    **Parity, round two (2026-10-18).** Round one ran the shared table and an adversarial
    set through both sides and found them apart on three inputs no export carries, each
    because the producer's `derive_status` took a claim the record format does not: an owed
    claim (`challenge` null), which it read as upgrading nothing; a claim with a `note` beside
    `complete`, `parts` and `challenge`, which it ignored; and one `location_key` on two
    locations, where it kept the last (so the answer depended on their order). WikiLean
    refused all three, as the agreed format says. The producer now refuses each too
    (`cflib.vocab.COVERAGE_RECORD_KEYS`: its `derive_status` takes exactly the claim a
    record carries, and an owed claim's holder passes none), and the shared table says so
    (cases C14 and X11 to X16), so WikiLean's semantics did not change. WikiLean added the
    pinned copy of that table, `derive_passage_status` to run it through the builder's own
    checks, and a named message for a null `challenge`.
    **Parity, round three (2026-10-18).** The lead set the reading of D1 that resolves Q1 to
    Q3, and both sides implement it (check 6; the skill's `references/vocabulary.md` §1.11,
    commit `3225e95`): a survived claim acts, for the derived status, exactly as one more
    `exactMatch` location, a fixed stand-in: a `proved` declaration on the theorem row and a
    definition on the definition row when every part is finished, a `statement_only`
    declaration on both rows otherwise. A part is finished when it is `proved` or data,
    unfinished when it is `statement_only`, `wanted`, `axiom_dependent` or a Prop-valued
    definition. Every part finished gives formalized on either row, any part unfinished
    gives statement only on both, an `example` or `other`
    passage takes the better row, and a claim that has not survived adds nothing. So Q1's
    claim takes both rows, Q2's data part is finished and its Prop-valued-definition part is
    not, and Q3's definition row has a statement-only case; the shared table's D04 (a
    definition passage's claim over data and a Prop-valued definition) moved from formalized
    to statement only. Confirming a claim confirms its parts: the producer exports a survived
    claim's `not_required` and `sampled_out` parts as `survived`
    (`scripts/locate/outcomes.py` `confirm_parts`), a part another verdict judged keeps it,
    and a part refuted or downgraded out of `hasPart` lapses the claim. WikiLean's view
    model applies the same rule (`confirmed_parts`) whichever value a part's records carry,
    and copies the records verbatim. The store's format did not change.
    **Review four (2026-10-18).** An adversarial review of both sides found the producer at
    `3225e95` confirming a claim's parts before a prominence overturn's reopen
    (`_apply_coverage` ran `confirm_parts` before `_reopen`), so a `sampled_out` part of a
    passage overturned to `subject` or `central` in the same run was exported `survived`,
    where WikiLean's `confirmed_parts`, handed the reopened part's `challenge_pending`,
    keeps it: the two sides' rules disagreed on that input, and the shared table had no
    case for it. The lead decided the reopen wins (check 6), and the producer now confirms
    parts after the reopen (formal-companion `4fc2aa0`; shared case P05, 118 cases).
    WikiLean's rules did not change, only their comments. What an older export reads as,
    part by part: one from before round three carries its parts' own values
    (`not_required` and `sampled_out` unconfirmed, a reopened part `challenge_pending`), and
    `confirmed_parts` shows exactly what a `4fc2aa0` export of the same run carries, so its
    parts read the same. One from `3225e95` itself carries
    `survived` on a reopened part, which WikiLean cannot tell from a confirmed one, so a
    `3225e95` set from a run with such an overturn is to be re-exported; the `3225e95`
    export WikiLean has built (`smoke-2-d1c`) has no claim, and its `4fc2aa0` re-export
    (`smoke-2-d1e`) has byte-identical records. Statuses are another matter: the builder
    re-derives every stamped status by the current rules (check 6) and refuses a
    disagreement, so an older set builds only if none of its passages turns on a rule that
    changed since (D1's data row, or a claim under Q1 to Q3). The three smoke-2 sets
    (`smoke-2-d1` from `ad0b7e8`, `smoke-2-d1c` from `3225e95`, `smoke-2-d1e` from
    `4fc2aa0`), which carry no claim, all build with the current builder, to the same
    status and challenge values for each of their 331 passages.
12. **The locate extension's keys, and F5's span** (2026-10-19). Until then the builder held
    the seal, the universe, the libraries and the coverage claim to their agreed keys, but
    read only the keys it used from the locate extension at its three places, so a new,
    misspelt or malformed key there (`spann` for `span`, a stray `x_note`) built without
    complaint and was published in the records. It now holds each place to the table in
    "The locate extension's keys", as section 3 always said, and names the key, its place and
    the record id. The keys are the ones proposal §2.7 shows, with the absence's `receipt`
    (note 3), `statement` (note 8) and `coverage` (note 11), so an export in the agreed
    format builds as it did. F5 (`scripts/locate/records.py` `subject_end` and `passage_span`
    at master, documented in the producer's `references/locate-documents.md` §6; first commit
    `722b30f`, on a branch since retired, and on master since) adds the passage's `span` and
    `layer_sha256` to every subject end; WikiLean accepts them as optional, both or neither, in
    exactly that shape, and checks them (section 3, above), so sets from before and after F5
    both build. `722b30f` also wrote `prominence` only when the passage's outcome row had one;
    WikiLean still requires it on every record (note 2), and the export at master refuses such
    a passage by name rather than omit the key (`records.passage_problem`). Checked on the real
    sets: `smoke-2-d1e`, its port re-export `smoke-2-p3` and an F5-shaped copy of `smoke-2-p3` (its 917 records given the
    spans of the run's outcome rows and the layer sha, each span verified against
    `span_sha256` on the run's own layers) build byte for byte the stores the previous
    builder built (31 files each), and the copy's misspelt `span`, a stray key at the record
    and subject levels, a malformed span and a wrong layer sha are each refused.
13. **Several libraries per repository** (agreed 2026-10-19). Proposal §2.3 gives a
    `brain_lib` token one repository but lets a repository carry several tokens: a consumer
    that needs one token per root (the Brain wants `Init` and `Std` apart) splits lean-core
    into two universe libraries at one commit, one root each. The builder refused that
    universe ("repo appears twice (one library per repository)"), and the producer's export
    then gave each absence two identical lean4 ends, which check 5 refuses too. The two
    sides agreed: WikiLean keys libraries by id and refuses only a true duplicate, the same
    library id or the same `(repo, root)` (section 3, above); formal-companion's absence
    names what was searched, one end per distinct `(repo, commit)` (`124a587`,
    `records.absence_ends`), and WikiLean checks exactly that set, each pair once. The view
    model was already keyed by library id (a location's `library`, `summary.by_library`,
    `counts.by_library`, the index rows' `libraries`, the universe file), so Init and Std
    are two libraries everywhere in the store, and an absence's `libraries` counts
    libraries, not repositories. The pages name libraries per id as well: two that would
    read alike, as two libraries of one repository always do, are told apart by their roots
    ("Lean core · Init", "Lean core · Std"; UI, "Libraries"). Checked on brain4's locate
    e2e (`tests/e2e/locate_synthetic/driver.py` at `124a587`) run over a split universe
    (`alpha`, and `init` and `std` at one `example.org/lean4` commit, with a location in
    `std`): the previous builder refuses its sealed set, this one builds it (12 files),
    with each absence naming lean4 once. The unsplit e2e set and the three smoke-2 sets
    (`smoke-2-d1`, `smoke-2-d1c`, `smoke-2-d1e`) build byte for byte the stores the previous
    builder built.

## 4. The store (v1)

```
articles/v1/current.json                    campaign, universe summary, record set (seal sha, set_id, byte_budget), totals (by status, prominence, library, importance), build time, index shard list
articles/v1/manifest.json                   every other file: bytes and sha256
articles/v1/universes/<universe-sha>.json   libraries by id (id, brain_lib, display name, repo, commit, toolchain, roots, licence SPDX id), exclusions
articles/v1/articles/<pageid>.json          the article's view model (below)
articles/v1/records/<pageid>.jsonl          that article's records, byte for byte as sealed
articles/v1/index/<shard>.json              title -> page id and summary counts, sharded
articles/v1/reader/<pageid>.html            optional (--reader-html): the article's pinned revision, its passages marked
```

- **Article view model** (`wikilean.articles.article/1`): `pageid`, `title`, `revision`,
  `source_id`, `wikidata`, `assessment`, `campaign`, `universe {id, sha256, frozen,
  libraries}`, `record_set` (the SEAL's sha256), `records_sha256`, `licence {text, mapping}`,
  `summary` (passages, locations, related, by status, by prominence, by library, by proof
  status, by relation) and `passages`. `licence` says which licence covers what the view holds:
  `text` is its records' subject licence (an SPDX id, `CC-BY-SA-4.0` for Wikipedia's text; every
  record of an article names the same one, or the set is refused; null for an article with no
  records), and covers the passages' `quote` and `text` and the reader's revision; `mapping` is
  `CC0-1.0` and covers the mapping (statuses, relations, locations, notes, provenance). A
  location's `signature` is an excerpt of its library's Lean code and keeps that library's
  licence, which the universe lists (its libraries' `licence`), not CC0. Each passage has `item`, `kind`, `section`, `prominence`, `quote`,
  `quote_capped`, `passage_ends_in_suffix`, `subject_uri`, `outcome` (located, absent or
  unresolved), `status`,
  `basis` (the `location_key` of the location that justifies the status, or null),
  `coverage` (the coverage claim, below, or null), and `locations`,
  each with `library, repo, commit, declaration, module, path, line, end_line, relation,
  proof_status, statement, rank, confidence, challenge, location_key, note` (`end_line`
  only when the record has one; `statement` is the Prop-valued-definition flag, false
  when the record omits it). `section` is the heading path (`[]` in the lead) and
  `prominence` marks the article's `subject` passages. A passage also
  carries `absence {universe, universe_sha256, libraries, receipt, challenge}` or
  `unresolved {challenge}`; for a passage that is unresolved because its only locations
  are `relatedMatch`, `challenge` is its rank-1 location's and the locations are kept. A
  passage with a coverage claim (decision D1) carries `coverage {complete, parts,
  challenge, status, basis}`: the claim as exported (`parts` are location keys, in the
  record's order), `status`, what the claim gives on its own (formalized or
  `statement_only` once it survived, null otherwise), and `basis`, true when the claim
  rather than one location justifies the passage's status (the passage's `basis` is then
  null). A part of a survived claim whose own `challenge` is `not_required` or
  `sampled_out` has `challenge: "survived"` in its location (check 6). A
  `relatedMatch` states something else (proposal §2.6), so it is counted
  in `related` and never as a location. `summary.by_library` (and `current.json`'s
  `counts.by_library`) splits each library's `locations` into `complete` (proved
  declarations and definitions) and `incomplete` (`statement_only`, `wanted`,
  `axiom_dependent`), so a library that only states results is never counted as one
  that proves them. Every library in the view model is a universe library id (a location's
  `library`, both `by_library` maps, the index rows' `libraries`), so two libraries of one
  repository (Init and Std, contract note 13) are two entries; a location also carries its
  `repo` and `commit`. A build with `--reader-html` (section 5) gives an article it has a
  checked body for `reader {sha256, bytes, marks, passages, unplaced[, images]}`: the sha256 and
  size of `reader/<pageid>.html`, its marks, the passages it places, the ids of those it leaves
  unmarked and, when it shows any, the files it shows with their licences and authors.
- **`quote_capped`** is true when the passage's quote is the capped head of a longer
  passage (section 3, "The quote is capped"), and false when it is the whole passage
  (`build_store.quote_extent`). A quote under 12 words is whole, because the passage ended
  first. The export right-strips the quote but hashes the span as it is, and a span can end
  in the space that ends its paragraph (29 of the 21,755 paragraphs of the producer's `wpm`
  source set do: 23 in a hair space, 5 in a space and a no-break space, 1 in a no-break
  space), so a quote of 12 words is whole exactly when the record's
  `span_sha256` is the sha256 of the quote followed by whitespace only; the builder tries no
  whitespace and every run of one or two whitespace characters, and a span that ends in a
  longer run reads as a head. The builder requires a 64-hex `span_sha256` (section 3);
  `quote_extent` called without one reads a quote of 12 words as a head, so a cut passage is
  never shown as whole. The quote itself is copied verbatim.
- **`passage_ends_in_suffix`** is true when the quote is such a head and the rest of its
  passage lies within the quote's `suffix`. The export adds a suffix when the quote is not
  unique on the page, and takes it from the page right after the quote, so it can hold the
  passage's end and run past it. The builder finds that end where the record's `span_sha256`
  is the sha256 of the quote followed by the suffix's first characters, or by the whole suffix
  and a whitespace run; whitespace alone after the quote makes the quote whole instead. It is
  false on every other passage (and, from `quote_extent` called without a usable
  `span_sha256`, on a head, whose passage then reads as going on past its suffix).
- **The passage's span.** The view model copies neither the subject end's `span` and
  `layer_sha256` (F5, section 3) nor its `span_sha256`; the builder reads them only for
  `quote_capped` and `passage_ends_in_suffix`, and there F5's span is a shortcut to what
  `span_sha256` gives. The span's text after the quote is
  `tail = (end - start) - len(exact)` code points long. A quote of 12 words whose tail the
  suffix covers shows the span's whole text, the quote and the suffix's first `tail`
  characters (which section 3 holds to `span_sha256`): it is whole when they are whitespace
  or none, and otherwise a head whose passage ends in its suffix. A tail that runs past the
  suffix by more than two characters is longer than any text the hash path tries, so the
  quote is a head. Only a tail that ends one or two characters past the suffix is left to
  `span_sha256`, the one field that says whether those characters are whitespace. So a set
  with `span` and `layer_sha256` builds to the same passages, summaries, index and universe
  as the same set without them (only the records' bytes differ, and the shas that name
  them). On `smoke-2-d1e`, `smoke-2-p3` and its F5-shaped copy (contract note 12), 322 of
  the 331 quotes are capped heads and none ends in its suffix. On the copy the span alone
  settles every passage the word count leaves (322 heads, 2 whole), none falls back to the
  hash, the hash alone gives the same answer for all 331, and the articles are
  `smoke-2-p3`'s but for the record shas. Where a span could contradict `span_sha256`
  undetected (a tail past the suffix while `span_sha256` hashes a shorter text), the span
  decides when its tail runs more than two characters past the suffix; within one or two,
  the hash decides, except that a span running past a suffix with any non-whitespace in it
  is never read as a whole passage (it proves the passage goes on past the quote). The Worker holds no layer to index, and a reader that does finds them in
  `articles/v1/records/<pageid>.jsonl`, which keeps every record byte for byte.
- **`summary.by_prominence`** counts the article's passages per prominence, `{subject,
  central, supporting}`, each `{passages, by_status}` with the five summary statuses, and
  `current.json`'s `counts.by_prominence` sums it over every article. The subject and
  central passages are the article's statements, and the pages count them apart from the
  supporting ones (worked examples, setup sentences): on smoke 2's real set, 204 of 331
  passages are supporting, and they set the headline of the bar over all passages.
- **Changing v1.** A field is added within v1 only when a store without it still reads
  correctly: the schema strings stay `…/1`, and the Worker treats the field as optional,
  falling back to what an older store allows. So `basis`, `coverage`, `statement`,
  `quote_capped`, `passage_ends_in_suffix` and `by_prominence` are absent from stores built
  before them; without `quote_capped` the page reads a 12-word quote as a head, without
  `passage_ends_in_suffix` it reads a head's passage as going on past its suffix, and without
  `counts.by_prominence` the dashboard leaves out its central-statements line (the article
  page counts its own passages). Removing or redefining a field, or changing the shard key,
  is a v2 store.
- **Index shards.** The key is the first hex digit of `sha256(title_key)`, which gives 16
  shards. `title_key` is the NFC title with underscores and Unicode spaces collapsed to
  one space and trimmed, the same in Python (`title_key`) and TypeScript (`titleKey`). A
  test in each language pins the same shards. Rows are arrays named by the shard's
  `fields` list, so a field can be appended within v1; the last one, `libraries`, lists
  the ids of the libraries that hold a location for the article, in universe order. A
  shard over 1 MiB is refused; a wider key would be a v2 store.
- **Determinism.** Output is canonical JSON (sorted keys, compact, UTF-8, newline) in a
  stable order: passages in record order, locations by rank, index rows by title.
  `built_at` defaults to SEAL's `created` (the latest one, for a build of several), so the same
  set always builds the same bytes; the dashboard then dates the record sets' export, not a
  build (section 8). Pass `--built-at` to date the build.

## 5. Building

```bash
python3 scripts/articles/build_store.py --record-set <set> --out <store>            # new directory only
python3 scripts/articles/build_store.py --record-set <set> --out <store> --check    # compare, write nothing
python3 scripts/articles/build_store.py --record-set <set> --out <store> --replace  # rebuild in place
```

The builder runs only under CPython 3.12.13 or a later 3.12 (CI runs a later 3.12;
`scripts/ci-python.sh` asserts 3.12): the reader's checks (`ReaderScan`) were proven against
that `html.parser`, and an older one reads attributes by another grammar (3.9's takes `=+`
between a name and its value), so the same checks could pass markup a browser reads
differently. Under any other interpreter it writes nothing and exits 3
(`interpreter_problem`).

Exit codes: 0 is success, 1 means `--check` found differences, 2 means the record set
was refused (the reasons go to stderr), and 3 means the builder itself failed (a bug, or the
wrong interpreter; nothing was written). Every check runs before any view is computed, and a value of the
wrong type (a list where a vocabulary word belongs, a number where `excluded` lists
belong, `1e400`) is reported and dropped rather than used, so a malformed set is always a 2,
never a crash that could be read as "`--check` found differences". A test sets nine
hostile values (null, a bool, numbers, a string, lists, objects) into some 90 fields of
the fixture in turn and requires a refusal or a build, never another exception. The tree
is staged next to `--out` and moved into place, and the previous store is moved back if
that fails, so a failed build leaves nothing half-written. `--out` is the store directory
itself: `--replace` refuses a directory that holds anything besides `articles/v1/`, so
pointing it one level too high (at `wiki/public`, say) cannot delete unrelated files. `--built-at <ISO-Z>` overrides the
build time (pass the same value to `--check`). `scripts/` is release-neutral, so the
builder can change without a release.

Several sealed sets build into one store when one batch arrives as several exports (two locate
runs need two tallies and two exports):

```bash
python3 scripts/articles/build_store.py --record-set <set-a> --record-set <set-b> --campaign <id> --out <store>
```

The sets must share `universe.json` byte for byte, the sourceset, `skill_commit`, the schema
and the status rule, and no page id or title key. `--campaign` (a campaign id, never a
`run:` token) names the combined build in `current.json`; `record_set` there keeps its shape
and is the first set's by `set_id`, and an optional `record_sets` lists every set (its seal
sha, set id, campaign, created, skill commit, byte budget). Each article keeps its own seal's
`campaign` and `record_set`, which the feedback route checks against.

### Supplements: five optional side files

A record set holds a passage's first 12 words, a declaration's name and the runs' ids, and
never the article, and says nothing of how accurate it is. Five optional side files add what it does not hold. Each is read only, each is checked against the
records it describes, and one that does not check out is refused (exit 2) like a broken record
set, never merged in part. A build without them is byte for byte the build it always was.

The side files' makers are operator tools that run beside the record sets, outside this
repository (the reader's maker runs the producer's own renderer). They write the schemas named
below; the builder trusts none of what they write and checks each against the records and the
seal as far as those can say. The runs' models, operators and dates are the one part they cannot
settle: the seal records none of them, so the provenance's are its maker's reading of each run's
own `run.yaml` and receipt, checked for consistency and against the seal's times and commits, until
the seal carries its runs' citations.

```bash
python3 scripts/articles/build_store.py --record-set <set> --out <store> \
    --passage-text <file> --signatures <file> --provenance <file> --reader-html <file> \
    --audit-summary <file>                                                              # any of them
```

- **`--passage-text`** (`wikilean.articles.passage-text/1`): `passages`, keyed by passage key
  (sha256(`<pageid>\n<span_sha256>`)), each `{id, span_sha256, layer, segments}`. `layer`
  must hash to the record's `span_sha256` (the passage's slice of the web-html/3 layer, UTF-8,
  no normalisation), `id` and `span_sha256` (and `pageid`, `span` when given) must be the
  record's, and `segments` must spell `layer` exactly: `{text}` and `{ref}` (a citation
  marker) are its text verbatim, `{tex}` (`display: true` for a display formula) is one
  formula, standing for its MathML text and the TeX annotation after it, which holds its TeX
  (style commands and whitespace aside), and `{tex, cut}` is a formula the passage's edge cuts.
  A passage with a checked entry gains `text: {layer, segments}`; one without keeps only its
  quote. An entry for no passage of the build is ignored, and a file that matches none is
  refused. An entry may also carry `before` and `after`, the text the page shows around a
  passage that is only part of a sentence: each `{layer, segments[, trimmed]}` (`before` may
  add `lead_in`, the sentence of the block before that introduces a list item), at most 2,000
  characters, its segments spelling its own `layer`. The record hashes only the passage, so the
  builder checks a context's shape and, where the record's quote has a prefix (or, for a quote
  that is the whole passage, a suffix), that the context meets the passage there. When the
  build also has the article's `--reader-html`, whose text it proves to be the revision's layer
  (its sha256 is the records' `layer_sha256`), and the record gives the passage's `span`, each
  context must be that layer's own text right before or after the span (a space where it meets
  the passage may differ), so the grey text the page shows is the revision's: a context one word
  off, or text from elsewhere in the article, is refused (`context_layer_problem`). The reader
  html is therefore checked first. Without it the side file's maker vouches for the rest.
- **`--signatures`** (`wikilean.articles.signatures/1`): `signatures[library][commit]
  [declaration]` = `{signature, kind, elided, proof_status, type[, deprecated_to]}`, and a
  `source` naming the corpus (`corpus_sha256`, `corpus_built`, `universe`, and
  `universe_sha256`, which must be the store's). The lookup is the card's exact library, commit
  and declaration; the signature must start with its Lean keyword and that declaration, and its
  `proof_status` must be the card's (else the corpus row is another declaration's). A location
  with one gains `signature: {text, elided[, type][, deprecated_to]}`: `text` is the Lean
  header (binders before the colon, no proof or value), `type` the whole corpus type when the
  header was shortened (`elided`).
- **`--provenance`** (`wikilean.articles.provenance/1`): `runs` by run id, `record_sets` by
  set id and `records` by page id, one entry per records line. Every set of the build must be
  described: its seal sha and campaign, and the runs its campaign token names, and the
  inventory run its locate run names (`inventory_run`, the run that chose the passages, each
  one's kind and its prominence), when it names one. Each run must check: its mode (locate,
  locate-challenge for the challenge run, whose `challenged_runs` name the locate run, or
  locate-inventory), its worker's model is the run's, its observed calls name only the models
  its roles were configured with (a model its own calls do not bear out is never cited), its
  operator, its window (`window_utc`, ending at its harvest), its producer commit (the seal's)
  and its universe. The windows are compared as times, never as strings, and must follow the
  record set: each run's harvest no later than the seal's `created`, the inventory harvested
  before the locate run starts, and the challenge started after the locate run it re-checked was
  harvested. Each record entry must name its line, shape, keys, locate run and challenge verdict
  and run. Every article gains `provenance: {locate, challenge[, inventory]}`, each `{run_id,
  mode, model, model_name, reviewer_model, reviewer_model_name, operator, started, harvested,
  skill_commit[, sampling]}`, and `current.json` gains `provenance.runs`, where a challenge run
  adds `rechecked`: `[re-checked, of]` for the Lean locations (related declarations aside) and
  the absences of central (subject or central) passages and of supporting ones, counted from the
  build's own verdicts, since the challenge samples the supporting passages and its rates alone
  do not say how much it re-checked.
- **`--reader-html`** (`wikilean.articles.reader-html/1`): `articles` by page id, each
  `{source_id, revision, html_sha256, layer_sha256, renderer, html, unplaced[, marks,
  passages, images]}`. `html` is the article's pinned revision (the sourceset's `body.html`, whose
  sha256 the records pin as `html_sha256`), sanitised for serving, with each passage's text in
  `<mark class="atl" data-p="<item hex>" data-status="<status>">`, split at element boundaries,
  a formula or citation marker wrapped whole, the passage's first mark carrying
  `id="p-<item hex>"` (the card list's anchor). Its maker places the marks from the
  producer's own renderer and proves them; the builder takes none of it on trust
  (`attach_reader_html`, `ReaderScan`):
  - the source fields must be the article's (its source id, revision, `html_sha256`,
    `layer_sha256` and renderer);
  - nothing a page must not serve: an element outside `READER_TAGS` (HTML phrasing and flow
    content, tables, figures, media and MathML; no script, iframe, form, object, base, link or
    meta); an attribute outside `READER_ATTRS` (the ones Wikipedia's parse HTML and its MathML
    use, each inert or checked, plus `data-*` and `aria-*`), so an event handler, another active
    attribute or one a browser gives an effect (`attributionsrc` sends a request to the hosts it
    names) is refused until it is reviewed and listed; a `src`, `srcset` or `poster` that is not
    https on `upload.wikimedia.org` or `thumb.wikimedia.org`, a srcset split at ASCII whitespace
    alone as HTML's parser splits it, and any `href`, `cite`, `src`, `srcset` or `poster` that
    holds a space character other than ASCII whitespace (U+00A0, U+3000 and the like: a browser
    keeps one inside the URL, which then resolves against the page, where `str.strip()` would
    drop it and check a clean Wikimedia URL); a link that is not an in-page
    fragment, an articles article or an http(s) page on another host (`reader_link_problem`): an
    articles article is `/articles/<pageid>` or `/articles/<pageid>/<slug>`, its slug one path segment as
    `articlesSlug` writes it and never a dot segment, which a browser resolves to another path of
    the site (`/articles/1/../../<path>`, and its `%2e%2e` form, is `/<path>`), and an http(s) link's host is
    plain ASCII, with no user, escape or backslash, and never the site's own
    (`READER_SITE_HOSTS`); a comment, a self-closing HTML element, a `target="_blank"` without
    `noopener`, an id or class the page's chrome or a passage anchor uses (the stylesheets put
    `.atlr-peek` and `.atlr-fab` fixed over the page), a `name` (`<img name="getElementById">`
    shadows the page's `document` properties), `autoplay`, `download`, a `referrerpolicy` other
    than `no-referrer` and a control character;
  - CSS read as a browser tokenizes it (`reader_css_text`: comments out, each string a
    placeholder holding its decoded value), so no escape hides a keyword: an escape outside a
    string is refused (`u\72l(` is `url(`, `@\69mport` is `@import`). A style attribute holds no
    `url()`, `image-set()`, `expression()`, block or fixed or sticky position; a TemplateStyles
    block no `@import` (its last statement needs no `;`), `image-set()`, `image()`, `src()`,
    `attr()` or `element()`, no `url()` whose quoted or plain value is off those hosts (its
    padding read as CSS reads it, ASCII whitespace alone, so a U+00A0 stays in the URL checked), no
    at-rule but `@media` and `@supports`, no nested rule, no `<`, and no selector whose
    `.mw-parser-output` is not one of its own compounds (inside `:not()`, `:has()`, `:is()`,
    `:where()` or an attribute value it matches outside the article:
    `html:has(.mw-parser-output) .banner` is the banner) or is followed by a sibling combinator;
  - markup a browser's tree builder would read differently from `html.parser`
    (`ReaderScan._tree`): inside MathML a `<style>`'s content is markup, not CSS, so a style
    element or any HTML element inside a formula is refused; every element must be closed by
    its own end tag, innermost first, and none left open at the end (an unclosed
    `<table><tr><td>` takes in the panel, an unclosed `<a href>` the rest of the page); no link
    in a link, block in a paragraph, heading in a heading, list item in a list item of the same
    list, table part outside its place or text directly in a table, and no end tag of a void
    element. The canonical bodies do none of this, so they build byte for byte as before;
  - its text, read as web-html/3 reads a body (the decoded text in document order, nothing
    from style, one LF dropped after `pre`, each White_Space run one space, the ends
    stripped), must hash to the records' `layer_sha256`: every character served is the
    pinned revision's;
  - each passage's marks, in document order, must cover exactly its span on that layer (the
    slice whose sha256 is its record's `span_sha256`, and the record's `span` when it has one)
    with at most a space between two of them; each mark carries its passage's status, none
    nests in another or crosses an element boundary, and only a passage's first mark carries
    its id; a passage with no status (an unresolved one) has no mark at all: it gets no
    highlight until it is read (the maintainer's decision, 2026-11-02), so `unplaced` lists it;
  - `unplaced` lists, as `{item, reason}`, exactly the passages the html leaves unmarked, and
    `marks` and `passages`, when given, are the html's;
  - no file that is not free (`reader_images`): every file the html fetches (an image's `src` and
    `srcset`, a video's `poster`, a `source`, a TemplateStyles `url()`) must be a Wikimedia upload
    URL (the original, a thumbnail or a transcode) of a Wikimedia Commons file or of an English
    Wikipedia upload, and `images` must name it, under its project, as `{project, licence,
    licence_id, licence_url, author, restrictions}`: the licence's printable name and its id as
    the MediaWiki API gives them (the id may be null for a Commons file), the licence's own page
    (the API's `LicenseUrl`, https on the licences' own hosts, `READER_LICENCE_HOSTS`, or null),
    the author as the file page credits them (the API's `Artist`, its markup stripped, plain
    text, or null), and the restrictions its file page sets beside the licence (the API's
    `Restrictions`, split at `|`: `trademarked`, `personality` and the like, `[]` for none); an
    English Wikipedia upload adds `categories`, its categories as the API lists them. An English
    Wikipedia upload may be non-free or free in the United States only, so one is shown only
    with a free licence id (public domain, CC0, CC BY and BY-SA of any version, the GFDL, the GPL
    and the like; `READER_FREE_LICENCE`) and with categories that make it free everywhere:
    `Category:All free media` and none of `Category:All free in US media`, `Category:Wikipedia
    files not suitable for Commons`, `Category:Wikipedia images using freedom of panorama` and
    `Category:All non-free media` (`READER_NOT_FREE_CATEGORIES`). A free licence id alone is not
    enough: a photo free in the United States only under freedom of panorama keeps its CC BY-SA
    id. A Commons file must name its licence, and a free one: by its id, or by its name when the
    API gives no id (the GPL, the LGPL, the GFDL, the "Attribution" licence, copyrighted free
    use, the public domain; `READER_FREE_LICENCE_NAMES`). Commons hosting a file is not a
    licence: a file page whose licence template is broken gives the API none, and the file is
    refused. A file under a licence that asks for its author's name with every copy (CC BY and
    BY-SA of every version, the GFDL, the GPL and LGPL, the Free Art Licence, "Attribution";
    `READER_ATTRIBUTION_LICENCE`) must name its author, and one whose licence has a text of its
    own (Creative Commons, the GFDL, the GPL and LGPL) must link it.
    The Wikimedia projects' marks and the Foundation's (logos, wordmarks and icons, the Wikipedia
    W) are refused wherever they are hosted and however they are listed, since the Foundation's
    trademark policy covers them whatever their copyright licence. A file is one by its name
    (`READER_WIKIMEDIA_MARK`) when the name starts with a project's or the Foundation's name as a
    word (Wikipedia-W-bold-in-square, Wikipedia_wordmark, Wiktionary_small, MediaWiki-2020-icon,
    Wiki_letter_w; "Wiki" alone counts, a longer word such as "Wikipedian" does not), or when it
    names a project or the Foundation beside a logo, wordmark or icon, in either order
    (Commons-logo, Notification-icon-Wikipedia-logo, WMF_logo); and whatever its name, a file
    whose page marks it `trademarked` is refused as a mark (`READER_BARRED_RESTRICTIONS`). The
    other restrictions limit a use, not the copy, and leave a file free. `images` names no file
    the html does not show. The maker gets each licence and restriction from the API and leaves
    out a file that is not free, and the marks (with the link or wrapper that holds nothing
    else); a TemplateStyles `url()` of such a file becomes `none`. It also leaves out a file
    the builder would refuse for its licence or its credit.

  A checked article gains `reader` (section 4) and the store `reader/<pageid>.html`, the
  html byte for byte; `reader.images` lists each file the html shows, `{file, project, licence,
  licence_url, author}` in file order, for the page's attribution (section 8). An entry for no article of the build is
  ignored, and a file that matches none is refused.
- **`--audit-summary`** (`wikilean.articles.audit-summary/1`): what an audit of the build's links
  measured, so the pages can give a figure that changes with a store build, never a release.
  Exactly `{schema, record_sets, audit, items, figures}`: `record_sets` the seal sha256 of each
  record set of the build, each once (an audit describes the sets it read, so a summary of
  another build is refused); `audit` the run that judged the sample, `{run_id, operator,
  window_utc: {start, end}, models: [{model, model_name}], blind, independent}` (a run id, a
  login, ISO 8601 UTC times, start first and no earlier than the latest seal's `created`, since
  an audit judges the sets as exported, one to four models, each a model id and its printable
  name, the provenance rule's grammar, and two booleans: whether its judges were blind to the
  runs and to each other, and whether they were independent of the runs they judged); `items`
  the number of items it judged; `figures` one to twelve estimates `{id, label, share, ci95,
  weights, n_eff, drawn, population}`: the share of the population judged right, from 0 to 1,
  and its 95% interval `[low, high]` around it, from `drawn` of the sample's items (at most
  `items`) standing for `population` (at least `drawn`), weighed by `weights` (what the draw
  was weighted by, plain text, or null for none) to a Kish effective sample size `n_eff`
  (above 0, at most `drawn`, and `drawn` itself without weights). A label is plain text
  (letters, digits, spaces and `,.()·/+:;'&%_-`, at most 160 characters) and an id a token,
  each id once. Any other key is refused. Every article and `current.json` gain `audit:
  {run_id, operator, started, ended, models, blind, independent, items, figures}`, which the
  "About this data" folds cite (section 8); without it they say the build carries no accuracy
  figure. The audit's maker writes the summary from the audit's own records.

Every article of a build with any of them carries `supplements` (each side file's schema and
sha256; the signatures' corpus too), and `current.json`'s `supplements` adds the counts
(passages with text, and `shown`, how many of them have a status, the ones the pages show;
locations and declarations with a signature; articles, passages and marks of the reader). All of these fields are
optional within v1: a reader that does not know them reads the store as before.

The fixture lives in `scripts/articles/fixtures/`. `make_fixture.py` writes `recordset/` (two
synthetic articles that cover every outcome, in exactly the agreed format: a lead
passage, two subject passages, a location without `end_line`, a closeMatch from a
challenge downgrade, a `challenge_pending` location, and two quotes at the 12-word cap,
one the capped head of a longer passage and one a whole passage, each with the
`span_sha256` of its passage's text) and `store/` is its built store. The Python
test pins both. The Worker tests and the browser harness read that same `store/`.

## 6. Store backend and publishing

The Worker reads through a one-method interface, `ArticlesReader.text(key)`
(`wiki/src/articles.ts`), which has four implementations:

| Reader | Backing | Wired today |
|---|---|---|
| `r2Reader` | R2 bucket bound as `ARTICLES` | yes, when `ARTICLES` is bound |
| `kvReader` | a KV namespace with the same keys | no (one-line change in `articlesReader`) |
| `staticReader` | the tree copied under `wiki/public/articles-store/`, served by `ASSETS` | no |
| `memoryReader` | an in-memory map | tests and previews |

**The binding is in `wiki/wrangler.jsonc`**: `ARTICLES` is a private bucket, with no r2.dev URL and
no custom domain, under the proposed name `wikilean-articles`, which the maintainer confirms when he
creates the bucket (section 14, item 2). Another name changes the binding and
`wiki/test/articles-binding.test.ts`, which pins it.

```jsonc
"r2_buckets": [
  { "binding": "ARTICLES", "bucket_name": "wikilean-articles" }
]
```

**The bucket must exist before the release that carries the binding is deployed.** Enabling
R2 and creating the bucket are account actions in the dashboard. `src/wikipedia.ts` notes that
R2 was not enabled on the Cloudflare account when the WP_HTML cache was built, and enabling it
may ask for a payment method. The free tier (10 GB, 1M writes and 10M reads a month) covers
the plane many times over. A bound bucket with nothing in it shows no data: until
`current.json` is uploaded, the plane answers as unpublished (below). The binding stays
optional in the Worker (`env.ts`), so a configuration or a test harness without it keeps
every route outside the plane as it is.

**Publishing** is an operator action, never the nightly and never the fleet host:

1. Build and `--check` the store from the sealed set on the operator machine.
2. Upload every object except `current.json` (`universes/`, `records/`, `reader/` when the
   build has `--reader-html`, `articles/`, `index/`, `manifest.json`), keyed by its path,
   overwriting what is there. That
   includes an existing `universes/<sha>.json`: the key is the producer's
   `universe_sha256`, which covers the libraries' ids, commits and index shas but not the
   metadata beside them (`display_name`, roots, toolchain, licence, exclusions), so the object is
   not content-addressed and a later campaign on the same frozen universe may change it.
   Each article object is self-contained, naming its own campaign, universe and record
   set.
3. Upload `current.json` last, then delete objects the new manifest no longer lists.

**`current.json` is the publish switch.** Until it is in the bucket, every route of the plane but the
feedback export answers 503 "articles store has no published build" and no article page shows a
box, whatever else the bucket holds, so a first upload goes live only when `current.json` lands.
Deleting `current.json` alone takes the plane down the same way, with no release: that is the
data rollback (or re-upload the previous build, which is why each build is kept). Each isolate
keeps what it read of `current.json`, its absence included, for a minute (section 7), so either
shows within a minute.

While a later upload is running, the previous `current.json` still in place, readers can see a
newer article next to an older index. Each article stays internally consistent, so the worst
cases are short-lived: a 404 from the index for a newly added page; an article whose new reader
page has landed before its new view, which names the old page's sha256, shows its card list
(`loadReaderHtml` logs `articles-reader-mismatch`) until its view lands; and records that have
landed before their view answer 503 from `/api/articles/records` until it does.

`scripts/articles/upload_store.py` runs these steps through R2's S3 API (usage:
`scripts/articles/UPLOAD.md`). It sends a reader store's pages (`reader/<pageid>.html`, from the
builder's `--reader-html`) after the records and before the articles, so the window above is the
one it leaves. It is a dry run unless given `--execute`. It runs the builder's `--check` first
and checks each upload by its ETag. It deletes only under `articles/v1/` and only once
`current.json` is in place, then checks every object against `manifest.json`. `--rollback-to`
re-publishes a kept build the same way, and `--unpublish` deletes every object under
`articles/v1/`, `current.json` first, which takes the plane to 503 within about a minute. It
takes the R2 token's S3 credentials from `WIKILEAN_ARTICLES_R2_ACCESS_KEY_ID` and
`WIKILEAN_ARTICLES_R2_SECRET_ACCESS_KEY`, or from a 0600 file, never from the command line. It
is a remote write, and only the maintainer runs it, from the operator Mac (section 14, item 3).

## 7. Worker routes

| Route | Answer | Cache-Control |
|---|---|---|
| `GET /articles[?q=]` | dashboard: totals by status, by library (complete and incomplete locations), by WikiProject importance; article list with the libraries holding each article, searched server-side through the index shards (200 rows shown). A `q` search takes the anonymous read budget of `/api/brain/*` and `/mcp` (per IP, an IPv6 client by its /64 (`limiterIp`), `MCP_LIMITER` else `BRAIN_API_LIMITER`, 120/min, key `articles-q:<ip>`) and answers 429 over it | none (HTML, like other pages) |
| `GET /articles/:pageid[/:slug]` | one article: the reader (section 8) when the store has its marked revision (`--reader-html`) and its bytes are the ones its view names, else the card list; the slug is cosmetic. Takes the article read budget (below) | none |
| `GET /articles/:pageid[/:slug]?view=cards` | the card list, with the view switch when the article has a reader | none |
| `GET /articles/universe/:sha` | the universe's libraries, commits and exclusions | none |
| `GET /api/articles/current` | `current.json`; takes the meta budget (below) | `public, max-age=60` |
| `GET /api/articles/article/:pageid` | the view model; takes the article read budget | `public, max-age=300` |
| `GET /api/articles/records/:pageid` | the raw records, `application/x-ndjson`; takes the article read budget | `public, max-age=300` |
| `GET /api/articles/universe/:sha` | the universe `current.json` names (a store holds one); takes the meta budget | `public, max-age=300` (not immutable: see section 6) |
| `GET /api/articles/index/:shard` | one of the index shards `current.json` lists (used by the dashboard's live filter); takes the meta budget | `public, max-age=300` |
| `POST /api/articles/feedback` | section 10 | none |
| `GET /api/articles/feedback/export` | section 10 | `no-store` |
| `POST /api/articles/tag` | section 10a: add a Lean declaration to a passage, or change the relation or note of a location | `no-store` |
| `POST /api/articles/tag/:pair_key/state` | section 10a: a soft delete that leaves a gravestone, and its undo | `no-store` |
| `GET /api/articles/overlay/:pageid` | section 10a: one article's community rows; takes the meta budget | `no-store` |
| `ALL /atlas`, `/atlas/*`, `/api/atlas/*` | **301** to the same path under `/articles` or `/api/articles/`, query string kept (`index.ts atlasRedirect`; section 0). Reads nothing, so it answers whether or not `ARTICLES` is bound | none |

Two paths under these prefixes are **not** this plane's and are untouched: the exact
`/api/articles` (the header search box's index of WikiLean's own articles, `index.ts`), which
the plane's `X-Robots-Tag` middleware skips by name, and the old graph stack's bare
`/api/atlas` and `/atlas_data.json`, which keep the plain 404 they have answered since
2026-08-04.

**The article read budget.** An article page reads its view model, its universe and its
marked revision from the store, hashes the revision and renders it; its two JSON views serve the
view model and the records. The three share one anonymous budget per client, kept like a `q`
search's but under its own key, `articles-read:<ip>` (per IP, an IPv6 client by its /64;
`MCP_LIMITER` else `BRAIN_API_LIMITER`, 120 a minute). It is taken before any store or D1
read. Over it, a page answers 429 with the plane's "Too many requests" page and a JSON view
`{ok: false, error}`, both with `Retry-After: 60` and `Cache-Control: no-store`, and neither
says how the budget is kept. The three small JSON objects (`current.json`, the universe, an
index shard) share a budget of their own, `articles-meta:<ip>`, kept the same way, so the
dashboard's live filter, which fetches every shard, never spends an article's. The dashboard
without a search and the universe page take none: they read only those small objects, through
the store-object cache (below).

With `ARTICLES` unbound, every route above except the export answers **503 "articles store not
configured"** (an HTML page or JSON, with `no-store`). Bound but without `current.json`, the
publish switch (section 6), every one of them answers 503 "articles store has no published build",
however much else the bucket holds, and article pages show no box. A store object that does not
parse, or lacks the shape the pages read (a truncated upload, a damaged object, a string no URL
can carry such as a lone surrogate), answers the plane's own 503 page or JSON, "an articles store
object could not be read", with the notice and the policy and `no-store`, never the site's
generic 500; the error goes to the log (`articles-store-error`) alone. The two JSON views check what
they serve as the pages do: the view model must parse as one, and the records must hash to the
`records_sha256` their view names (so missing or cut-short records, or records uploaded ahead of
their view, answer that 503, not a 200 that browsers and caches keep for five minutes). An
unknown or malformed page id, sha or shard answers 404, and a sha or shard that `current.json`
does not name is a 404 that reads nothing. `articles` was already in `RESERVED`, so no article can take the name. The
security middleware (`src/security.ts`) adds its headers to every one of these responses, and
every page of the plane also enforces `articlesCsp` (section 8).

**Indexing.** Every answer under `/articles` and `/api/articles` carries `X-Robots-Tag: noindex`,
whatever its status (the site's 404 for a path no route of the plane takes among them), and every
page also has `<meta name="robots" content="noindex">`: a reader page copies a whole pinned
Wikipedia revision, a card list its passages, and the JSON views the same text. Links on the
pages are still followed, and `robots.txt` still lets crawlers fetch the paths, which is how they
see the header. WikiLean's own article pages, with the summary box (section 9), stay indexable.

The store's small objects, `current.json`, the universe and the index shards, are cached per
isolate for 60 seconds (`INDEX_CACHE_MS` in `src/articles.ts`), each with its text and its parsed
value (a missing object too), so no route re-reads or re-parses them on every view: the
dashboard, the universe page, the article box and the three JSON routes, which serve the cached
text, cost at most one read of each a minute per isolate. A newly published store, a withdrawn
one or a repaired object reaches them within a minute, the staleness `/api/articles/current` already
allows. An object that was read but does not parse is kept as that failure for the same minute,
so the dashboard and the universe page, which take no read budget, read a damaged object at most
once a minute too; a failed read (R2 answering with an error) is never kept. An article page links its WikiLean article when one has its
Wikipedia title. Each isolate keeps those answers, "none" included, for the same 60 seconds
(`SLUG_CACHE_MS`), at most 1,024 titles (`MAX_SLUG_CACHE`, the one read longest ago dropped
first), so a page read again within a minute costs no D1 read; a new WikiLean article gets its
link within a minute. That bounds repeat views of one page, not a crawl across titles, so
migration 0016 (`wiki/migrations/0016_articles_wikipedia_title.sql`) indexes
`articles.wikipedia_title` and the lookup reads the matching rows alone instead of the whole
table. Nothing requires it: the query and its answers are the same without it (only the rows D1
reads change, a scan of the table instead of a search of the index), the index is not unique, so
no write to `articles` depends on it, and no test, release check or promoter step looks for it
(the promoter's D1 schema check, `D1_REQUIRED_COLUMNS`, covers 0014's columns alone). So the
operator may apply it at any time, before, with or after the release that ships the plane, after
a D1 backup like the others (`wrangler d1 execute wikilean --remote
--file=migrations/0016_articles_wikipedia_title.sql`, `CLAUDE.md`, "Site / D1"); it is idempotent,
so a second run is a no-op.

## 8. UI

The UI is server-rendered (`wiki/src/articles-render.ts`) with one small vanilla enhancement
(`site/assets/articles.js`) and one stylesheet (`site/assets/articles.css`), both copied by
`wiki/scripts/build-public.ts`. The pages work without JavaScript, except Confirm/Report
and the live list filter.

- **Coverage strip.** Under a "Coverage" heading: passages checked and Lean locations, a
  coverage bar whose segments are filled with patterns (solid, hatched, dotted,
  cross-hatched), and filter chips by status and by
  library. Each status chip carries a swatch of its fill, so the chips are the bar's
  legend. Status and library are two facets combined with AND ("Showing 0 of 4 passages:
  Statement only, in Mathlib."); pressing a pressed chip clears its own facet. A chip with
  a zero count is shown flat and inert, like the dashboard's totals legend. A library with
  only related declarations is labelled "related only"; the Where row has a chip only for a
  library that holds a location or a related declaration (the article's
  `summary.by_library` names no other), so it never shows a chip of zeros. After the Status
  row, which is the bar's legend, so that the first figures under the bar are the bar's, and
  before the Where row, a "Central statements" line says how many passages state the
  article's subject or a named result or definition (prominence `subject` or `central`,
  proposal §2.2), how many are supporting, and the central ones by status ("Central
  statements: 19 of the 37 passages state the article's subject or a named result or
  definition; the other 18 are supporting (examples, setup). Of these 19: ✓8 formalized,
  ◔9 partial, ✕2 not formalized."), from the passages' prominence. The bar over all passages
  stays: the supporting passages (worked examples, setup sentences) are most of an article's
  passages and would otherwise set its headline. The commas between the counts are elements
  of their own, so the summary box's `.articles-mini + .articles-mini` margin does not widen the gap
  after them. A store whose passages carry no prominence gets no such line. When the article has subject
  passages (prominence `subject`), a "Subject" row names each one (its kind and quote,
  linking its `#p-<hex>` block) with its status pill; a quote that is a capped head
  (`quote_capped`) is followed, after its closing quotation mark, by an ellipsis. The meta
  line names the campaign, or for a run outside any campaign, the runs ("run L with
  challenge run C, outside any campaign").
- **Passage block.** Under a "Passages" heading, one `<h3>` per passage with its kind,
  prominence and section path, beside a status pill (icon and word). The heading's pieces
  are flex items separated only by a gap, and the section's "§ " is CSS generated content,
  so a visually hidden " · " (`articles-sr`) sits between them and the heading's accessible
  name reads "Theorem · central · § Statement" rather than running the pieces together. A
  subject passage carries a "Subject of the article" tag in place of the prominence word,
  the class `is-subject` (an accent frame) and `data-prominence="subject"`. A located passage
  then says in visible text which location gives it its status, for example
  "Formalized: Mathlib states this exactly (proved).", naming the store's `basis`, not
  simply the rank-1 location. A partial status whose basis is an exact, close or more
  general match says why, since the card beside it reads as a strong match: a definition,
  construction or notation passage is formalized only by a Lean definition ("Partial: Mathlib
  states this exactly, but as a proved theorem, not a definition; a definition passage is
  formalized only by a Lean definition."), and under status rules 1 and 2 a more general
  definition leaves a theorem partial. A survived coverage claim (decision D1) adds a line with
  its parts listed in the claim's order (library, declaration, proof status): "Formalized
  by 2 parts together:" when every part is finished, or "Stated by 2 parts together, not
  all of them finished:" (check 6). The builder gives every survived claim one of the two;
  for a store built before parity round three, whose claim may carry no status, the line
  reads "Covered by 2 parts together, not all of which count toward the status:". A `/2`
  bundle adds its relation when it is not exact ("Formalized by 3 parts together (more
  general):") and lists under each part the conjuncts it states. A part the claim confirmed
  reads as re-checked ("it survived") in its card's Details. When the claim is the basis that line is the status sentence;
  otherwise it follows the sentence. A claim that did not survive shows no line, and its
  parts are ordinary "states part" cards. Then the quote with its context, and a link to the passage
  in the pinned Wikipedia revision (a text fragment). The quote is verbatim, with one exception
  in how it is drawn: the producer's text layer is the article HTML's `textContent`, so it
  holds each formula twice, as MathML token text ("w : G ~ → G") and then as its TeX
  annotation ("{\displaystyle w:{\tilde {G}}\to G}"). The page (`wiki/src/articles-math.ts`)
  draws each formula once, from its TeX (Unicode letters and symbols, `<sup>`/`<sub>`,
  stacked fractions; the TeX is its title), taking as its tokens the words in front of the
  annotation whose letters and digits match the TeX's. A formula the export's cut truncated
  shows its whole tokens when they hold more than the TeX's head, and a run of tokens at the
  cut whose annotation was left out is drawn from the tokens. The JSON and JSONL keep the
  quote as the record has it. The text fragment is built from the quote's prose only: the
  whole quote when it holds no formula (as before), else its first specific run of words
  (three, or two of 15 characters) as the start and its last run as the end; a quote with no
  such run links to the revision without a fragment. An ellipsis after
  it means that the passage continues, and nothing else: when the quote is the capped head of
  a longer passage (`quote_capped`), an ellipsis follows it outside the highlight, after any
  suffix, with "(the passage continues)" for a screen reader, unless the rest of the passage
  lies within that suffix (`passage_ends_in_suffix`), which the card shows. A suffix is context
  and has no ellipsis of its own, even when it stops mid-sentence (a prefix keeps its leading
  one). The blockquote is `data-quote="head"` for a capped head (else `"whole"`), and a note
  under the Passages heading reads "Quotes show at most the first 12 words of a passage (the
  record format's quotation cap; a formula's MathML text counts toward it). A … after the
  highlighted quote means the passage continues; “Read it in context on Wikipedia” shows the
  rest. Hover a formula for its TeX." A legend under it shows each relation badge beside what
  it means (exact match: states the passage exactly; close match; more general; special case;
  states part; related: never counted), with the record's term (`exactMatch` …) as each
  badge's title. A store built before `quote_capped` gets the word count: a 12-word quote
  reads as a head. The producer's item
  id (`In-text #<hex>`) is a `data-item` attribute, not reader-facing text. Passage
  permalinks (`#p-<hex>`) clear the sticky header (`scroll-margin-top`).
- **Location card.** One card per location, and it leads with the Lean code: the
  declaration (namespace faint, leaf bold, wrapping at `.` and `_`), then its module and
  lines ("lines 612–640", or "line 88" when the record has no `end_line`). Beside the
  declaration, the relation is a badge in words (exact match, close match, more general,
  special case, states part, related), its stroke solid for exact and close, dashed for more
  general, special case and part, and dotted grey for related. Under the declaration, when the
  store has its signature (`--signatures`), the Lean header is a monospace code block that
  the page flows at the card's width, breaking only between the keyword and name, the binder
  groups and the hypotheses (each with its arrow), with a four-column continuation indent (two
  on a phone), so
  no binder or hypothesis is split unless it alone is wider than the card; a header shortened
  to fit keeps the whole type, as Lean prints it, under a "Full type" fold, and a deprecated
  name says what replaced it. A note under Passages says once what the code is: the type as
  Lean prints it, laid out as a header (the corpus keeps no hypothesis names, so hypotheses
  show as arrows), in Mathlib's notation, and not the source text. A store without signatures
  shows the name alone (the export names the declaration only). Then the
  library chip, `@<sha7>`, a proof-status badge and "Source @<sha7>" linking
  `<repo>/blob/<commit>/<path>#L<line>-L<end_line>`, or `#L<line>` without an `end_line`.
  `statement only`, `wanted` and `axiom-dependent` are drawn as amber dashed alerts (the
  statement-only colour, not the not-formalized red) and are never hidden. Then the
  record's note, labelled by what it explains for that relation ("Note" for an exact match,
  "Why close, not exact", "Why more general", "Why a special case", "Why only part", "How it
  differs"), and the provenance under a closed **Details** fold.
- **The notice (the maintainer's decision, 2026-11-02).** Every page of the plane (the list, a
  reader, a card list, the universe page and the 404, 429 and 503 pages) has one line under its
  header, exactly "Preliminary: these links have not yet had an independent audit."
  (`ARTICLES_NOTICE`); the reader puts it at the top of its article column, beside the sticky
  panel rather than above it, so the panel still fills the window below the header. It
  states the dataset's audit status, so it is never a badge on a card, and it is these pages'
  own line: the article box (section 9) does not carry it. The notice changes only with a
  release.
- **Accuracy.** What an audit measured is in the "About this data" fold (the dashboard's "About
  this build"), first, and changes with a store build. With an audit summary in the store
  (`--audit-summary`, section 5) the fold says how the audit stood to the runs it judged ("blind,
  not independent"), gives each figure as the estimate it is, with its interval and what it rests
  on ("…: 80% (95% interval 62–91%), from 16 drawn of 400, weighted by relation and library, an
  effective sample of 12.5"), and cites the run that measured them (its id, operator, window and
  models, as the provenance rule cites a run). Without one it says "This build carries no
  accuracy figure from an audit of its links." (`UNMEASURED`), a statement about the build, which
  says nothing of whether an audit of its links exists.
- **Licence (the maintainer's decision, 2026-11-02).** The reader's footer says that the page is
  an adaptation of the pinned revision, shared under its licence (the article's `licence.text`,
  CC BY-SA 4.0 for Wikipedia's text, the one link marked `rel="license"`): it names the article
  and links the revision, its history (the contributors) and the licence, then lists what the
  articles changed (the passages highlighted, a panel of Lean cards added with their links, the
  revision's links pointed to Wikipedia or to a page of this plane, the Wikimedia logos and any image that
  is not free removed, its navigation boxes (rows of links to other articles) and any section
  edit links hidden) and says that images and media keep their own licences. The card list's
  footer credits the same revision for the passages it shows, and every other page's names the
  licence of its quotes. Every footer ends with the mapping's licence: WikiLean's CC0, distinct
  from the article text, and says that the Lean code on the cards is quoted from its libraries
  under each one's own licence, which the universe page lists, not under CC0.
- **Image credits.** Each image and media file keeps its own licence, and the reader credits
  each as that licence asks: a fold in its footer lists each file the revision shows
  (`reader.images`), named and linked to its file page on Wikimedia Commons or English
  Wikipedia, with its author as that page credits them (the builder requires one wherever the
  licence asks for the author's name: CC BY and BY-SA of every version, the GFDL, the GPL and
  LGPL, the Free Art Licence, the "Attribution" licence), its project and its licence, linked to
  the licence's own page wherever the licence has a text of its own (https only).
- **Provenance (the maintainer's display rule, 2026-10-19).** Specific, stored, and hidden
  until a reader opens it: never a generic label such as "AI pipeline". A card's Details
  name the locate run that found it (from the campaign token `run:<locate>+<challenge>`)
  with its rank and confidence there, the challenge run that re-checked it and its verdict
  ("Re-checked in challenge run chal-…: it survived"), the record set and its export date and
  producer commit (a `/2` store's `seal`), and its location and pair keys. The page's "About
  this data" fold (the dashboard's "About this build") holds the runs, the record set, its
  status rule and its seal sha, and the dashboard's dates the build: "built <day>" when the
  builder was given `--built-at`, else, since `built_at` is then the latest seal's `created`,
  "from record sets exported <day>". The seal records no model and no operator; a store built with
  `--provenance` names them, so a card's Details read "Found by Model X (model-x) on 31 Oct–1 Nov
  2026 (UTC), locate run loc-… by jack: candidate 1 …" and "Re-checked by … on 1 Nov 2026 (UTC),
  challenge run chal-… by jack: it survived" (or "Not re-checked: sampled out of challenge run
  chal-… by jack, in which … re-checked a sample on …"); an absence's Details say who searched
  and who upheld it, and the "About" folds give
  each run once, to the minute, with its worker's and reviewer's models (first the inventory run
  that listed the passages, with each one's kind and prominence, when the store names it), and
  what the side files merged; the challenge run's line says what it re-checked (every location and absence
  on a central or subject passage, a seeded sample of the supporting ones at its rates, and the
  totals: on an article page counted from the page, on the dashboard from `rechecked`). A model
  id never breaks across lines at its hyphens. Without it the folds say that the record set does
  not record the model or the operator rather than naming any. No voting data is shown:
  Confirm/Report shows only the reader's own verdict.
- **Passage text.** A store built with `--passage-text` shows each passage with a checked entry
  in full, centrally, in place of its quote (`data-quote="full"`): prose and citation markers as
  the revision states them, every formula drawn once from its TeX (a display formula on a line
  of its own). A passage that is only part of a sentence, with `before`/`after` in the store, is
  highlighted and the rest of its sentence is shown in grey around it (a list item's lead-in on
  a line of its own). A note under the Passages heading says how many are shown in full, how
  they were checked and what the grey text is; the 12-word note stays only when a passage still
  shows a capped quote, and the footer's licence line names the JSON download's `text` fields
  beside its quotes. The "About" folds count the text shown of the passages shown: a passage
  with no status is in neither number (on the dashboard, `supplements.passage_text.shown` of the
  passages with a status).
- **Formulas.** Drawn once from their TeX (`articles-math.ts`): matrices, cases and the align
  family as rows and columns between delimiters that stretch over the rows (a row's spacing
  such as `\\[4pt]` and alignedat's column count are never shown), a base's sub- and
  superscript stacked, an accent drawn over its letter at a size that reads at 1x, and a
  display formula broken only after a top-level relation. A formula wider than its passage (a
  phone) scrolls sideways inside its own box, which takes keyboard focus; the page never does.
- **Absence:** "Searched N libraries in universe <id> (<frozen>) — not found", linking the
  universe page; the receipt's counts and the challenge verdict are under its Details. A
  Prop-valued definition (the `statement` flag) is badged "Prop definition" and described as
  stating the claim without proving it.
- **A passage with no status** (an unresolved one: the locate run settled none, or found only
  related declarations) **is set aside until someone reads it** (the maintainer's decision,
  2026-11-02, after PD5: a passage the inventory may have cut wrongly gets no highlight and no
  status until it is read). Neither the card list nor the reader lists, highlights or labels it,
  it has no status chip or legend entry, and the counts and the bar leave it out, its related
  declarations with it; the Coverage line says how many were left out ("1 more passage has no
  status yet; it is left out until someone reads it."). The dashboard and the article box do the
  same: no "undecided" status, chip, column or bar segment, their passage counts (the totals, the
  importance table and the article list, here and in `articles.js`'s live rows) leave it out, and
  they say how many more passages there are, so a box and its article's page give one count. The
  JSON downloads keep it (`unresolved` in the counts and the index).
- **Dashboard.** The by-library table splits locations into complete (proved or a
  definition) and incomplete (statement only, wanted, axiom-dependent), so a library that
  only states results never reads like one that proves them. Its rows are the libraries
  that hold a passage or a related declaration, sorted by passages (most first), then by
  complete locations (most first), then by name; every other library of the universe was
  searched and holds nothing in this build, so it is listed by name with its commit in a
  `<details>` after the table's column notes ("39 more libraries searched, with no
  locations in this build" on smoke 2, whose universe has 48) rather than as a row of zeros.
  The rows come from `counts.by_library`: a library it names that the universe does not list
  (the route passes no universe when its object cannot be read) is a row under its id, with
  no commit, so the page never says that no library holds anything when one does. The Totals
  section adds the "Central statements" line under the bar and its legend ("Central
  statements: 127 of the 331 passages, across 10 articles, state an article's subject or a
  named result or definition; the other 204 are supporting (examples, setup). Of these 127:
  …" on smoke 2), from `current.json`'s `counts.by_prominence` (absent from older stores,
  which get no line).
  The article list has a Libraries column (the index rows' `libraries`, as short names).
- **Libraries** (`wiki/src/libraries.ts`, contract note 6). Names come from the seeds
  (Mathlib, Lean core, Batteries, TauCeti, Formal Conjectures, atlas-lean),
  then the FormalFrontier organisation pattern ("FormalFrontier · <repo>"), then
  owner/repo for any other GitHub repository. A universe entry's `display_name` is used
  only for a repository the seeds do not name, only when it is printable ASCII, and never
  when its skeleton contains a seeded library's name (contract note 6). A universe's
  `blob_url_template` is ignored (the builder refuses it): every source link is GitHub's
  blob scheme for the library's own repository, a non-GitHub repository gets none, and
  owner or repository names `.` and `..` get none. The universe page links a repository
  only when it is `github.com/<owner>/<repo>` and names any other as text. A record's
  `path` is relative to the repository root, so nothing is prefixed to it (Lean core's
  paths already start `src/`). Every page shows libraries per library id
  (`libraryInfos`), as the store keys them, so libraries split from one repository (contract
  note 13) are separate chips, cards, filters, table rows and universe rows. Libraries that
  would read alike (one chip label, as libraries of one repository always do) are named
  apart by their roots: "Lean core · Init" and "Lean core · Std". A root is shown without
  a Lake glob's `.+` or `.*`, several are joined ("· Init, Std"), and when a root is not a
  plain ASCII module name, or the roots do not tell the libraries apart, the library ids
  are shown instead ("Lean core · init"). The name before the "·" is always the
  repository's, so a root cannot relabel a library.
- **Accessibility.** Status is never shown by colour alone: each status has an icon, a word
  and its own stroke (solid, dashed, dotted, double, grey groove). Chips are real
  buttons with `aria-pressed`, and filter results are announced through a status region.
  The layout has 16px gutters on phones and no horizontal page scroll; a wide table
  scrolls sideways inside a labelled, focusable region ("Table: …"), with its row labels
  sticky below 720px. Only `/articles` itself marks the Articles nav link `aria-current="page"`.
  Colours are the stylesheet's tokens, so dark mode follows the site toggle and the OS
  preference. The browser tests (`wiki/e2e/articles.spec.ts`) run axe on the coverage page
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
  segments get no link. The pages have no inline event handlers, and `articles.js` writes
  only through `textContent` and `createElement`. The pages keep two inline scripts
  byte-identical to the other shells (the theme bootstrap and the header search), and the
  coverage bar uses `style="width:…"`.
- **Content-Security-Policy.** Every page of the plane (`articlesHtml` in `src/articles.ts`: the list,
  the reader, the card list, the universe page and the 404, 429 and 503 pages) enforces a
  policy of its own, `articlesCsp` (`src/security.ts`), for the origin it was served from, which
  the security middleware lists beside the framing ban (`<policy>, frame-ancestors 'none'`: a
  browser enforces both). `default-src 'none'`; scripts only the plane's two files by URL
  (`ARTICLES_SCRIPT_PATHS`, `/assets/articles.js` and `/assets/articles-reader.js` on the page's origin,
  whatever their `?v=`; not `'self'`, which would admit every other script of the site, the
  editor's, the review page's and the Brain's), the Cloudflare Web Analytics beacon
  (`static.cloudflareinsights.com`) and the shell's two inline scripts by sha256, so no other
  script file, inline script or inline handler runs; images and media from the site and Wikimedia's
  upload hosts only (the hosts the builder lets a revision fetch from, `READER_FETCH`); styles
  from the site and inline (the revision's TemplateStyles and style attributes, which the
  builder checks, and the bar's widths); fetches to the site and `cloudflareinsights.com`;
  forms only to the dashboard's search (`ARTICLES_FORM_PATH`, `/articles`, the one form one of these pages
  has); no base, object or frame. Violations go to `/api/csp-report`. A reader
  page serves the stored revision as the builder checked it, not re-sanitised, so the policy is
  the second barrier: were the stored bytes ever hostile, no script but the plane's own would
  run and nothing would load from another host. `test/articles.test.ts` recomputes the two hashes
  from every page of the plane, so changing either script fails until its hash in `security.ts` is
  updated. Anything else Cloudflare injects into a page as inline script would be refused and
  reported, so after a deploy the Workers Logs' `csp-report` events for `/articles` pages are the
  check. The old article page's summary box is not one of these pages and keeps the site's Report-Only
  policy. `articles.css` and `articles.js` are static assets, so they get the security headers from
  the generated `_headers` file, like every other asset.

### The article reader

With the store's marked revision (`--reader-html`), `/articles/:pageid/:slug` is the article
itself, as WikiLean's article pages and the PDF overlay viewer show a source with its
formalization on it (`articlesReaderPage`; its look and behaviour are `site/assets/articles-reader.css`
and `articles-reader.js`, both copied by `build-public.ts`). Before it serves the revision the page
checks its bytes against the view's `reader.sha256`; a missing or different file serves the card
list, which `?view=cards` always gives.

- **Layout.** WikiLean's header with a second row: an "Article | Card list" switch (which
  carries the selected `#p-<hex>` across), the card list's status and library chips, "Central
  only" and "Dim other text". Below it, the revision in WikiLean's `.wl-article-body` (its
  headings, figures, tables and MathML formulas as the parse output gives them, links to
  Wikipedia or, for an articles article, to its reader), and a sticky panel beside it,
  `clamp(380px, 34vw, 520px)` wide. Below 1000px the article is the page and the panel a bottom
  sheet (46% of the screen, 52% below 600px) that a tap opens; a "N passages" button opens it on
  the index, from its top. With the sheet open the header drops its second row (the switch and
  chips come back when it closes), the sheet's handle, nav and ✕ share one strip, and it leaves
  out the passage's text, which the article shows just above it; below 600px the header leaves
  out the search box and the "Articles" title link (the breadcrumb names it). Every
  in-page jump, Wikipedia's own `[1]`, `↑` and section links among them, stops below the sticky
  header (`scroll-padding-top`, set to its measured height), and the note a link jumped to is
  marked. Without the script the panel follows the article and `#p-<hex>` still scrolls to
  the passage's first mark.
- **Formulas.** Each formula is its MathML, in STIX Two Math (or another font with an OpenType
  MATH table; Chromium's `math` family is Times on macOS, which stretches no bracket and
  enlarges no operator). `articles-reader.js` (`fixMath`) then draws in the page what MathML Core
  leaves out of Wikipedia's MathML: `mathvariant` (`\mathbb{R}` becomes U+211D ℝ, and the
  script, fraktur, bold, sans-serif and monospace letters their Mathematical Alphanumeric
  Symbols), the named spaces (`\,` is `thinmathspace`), the table attributes (`columnalign`,
  `columnspacing`, `rowspacing`, `rowlines`, `columnlines`: aligned rows line up on `=`), TeX's
  thin space around an operator name (`\sin x`, never before a bracket), TeX's ordinary `/`,
  `|` and signs, and a prime on the line rather than raised twice; `articles-reader.css` draws
  bold operators and `menclose`. It changes only the page's DOM, after the builder has proved
  the served bytes: the TeX annotation, the marks and the layer are untouched.
- **Highlights.** Each mark carries WikiLean's status wash and the plane's stroke under every
  line: formalized solid green, statement only dashed amber, partial dotted amber, not
  formalized a double red line, so status is never colour alone; a passage with no status has
  no mark (a mark from an older store reads as plain text). A passage over several elements is all its marks; hover darkens all of them and
  a desktop pointer gets a one-line peek ("✓ Formalized · Asymptotics.IsEquivalent +1 · more
  general"). Selecting a passage (click, tap, Enter or Space on its first highlight that is not
  link text, a row of the index, the stepper, ← and →, a `#p-<hex>` link) strengthens its wash,
  draws the viewer's 4px bar in the margin from its first line to its last, writes `#p-<hex>`
  with `replaceState` and fills the panel. A filtered-out passage reads as plain text.
- **Panel.** With nothing selected: the passages in the article's reading order under its
  section headings (status, first words, lead declaration and "+N", or "not found"), the
  coverage bar and central-statements line, the legend of the four highlights and of the
  relations, "About this data" and the data links. With a passage selected: "← All passages",
  a stepper "‹ 2 of 58 ›" over the passages the filters show, and the passage's section, which
  the server renders hidden for every passage: the card list's own block (heading, status pill
  and sentence, the passage's text clamped to three lines with "Show all", then exactly the
  card list's cards, Lean code first, the relation badge, the note, Details closed, Confirm or
  Report), the absence box, related declarations folded, and "Open in card list",
  "Copy link" and "Read on Wikipedia". `articles.js` binds Confirm and Report there as on the card
  list, and its "Sign in to confirm or report" comes back to the passage (`returnTo` carries its
  `#p-<hex>`). Esc closes the peek, then leaves the passage (focus returns to its highlight). A filter
  is kept per article for the tab's session (`atlr-filter:<pageid>`), so another article opens
  unfiltered. A smooth jump to a passage is placed again where it ends when the page moved under
  it (an image that does not load gives way to its description, in the image's own box).
- **Security.** The revision is the builder-checked html (section 5); everything around it is
  escaped as on the card list. `articles-reader.js` fetches nothing and writes only through
  `textContent`, `setAttribute`, `style`, `classList` and the MathML elements it creates
  itself, reading what it needs from data attributes.
  The page loads only same-origin scripts and stylesheets; the revision's images load from
  Wikimedia's upload hosts when the viewer is online, and give way to their description when
  they do not load. The article is paint-contained (`contain: paint` on `.atlr-article`):
  nothing of the revision paints outside the article's box, it is a stacking context of its own
  and the containing block of anything positioned in it, so an absolutely positioned element of
  the revision, whatever its z-index, never covers the header, the panel, the page head or the
  footer (the builder already refuses fixed and sticky positions and the page's own classes). A
  table wider than the article is cut at its edge on a wide screen, where it ran under the panel
  before; on a phone it scrolls in its own box, as it did.

## 9. Article pages

When `ARTICLES` is bound and the article's Wikipedia title has an index entry, the article
page shows a compact box above the body: passages checked, counts by status and "See
where →" linking `/articles/<pageid>/<slug>`, whose page carries the plane's notice under its
header (section 8). The page's header badges count WikiLean's own
annotations, so the box says its counts are separate from them ("Separately from this
page's annotations, WikiLean checked N passages of Wikipedia revision R…"), and when the
articles checked another revision than the page shows it adds "This page shows revision
R′.". Its counts are the passages that have a status, as the article's own page in the plane counts
them: a passage with none yet is in no count and has no status, and the box adds how many more
there are ("1 more passage has no status yet."), or, when no passage has one, that the plane
found N passages, none with a status yet. `serveArticle` adds it **per request**, like
the staleness banner (`pages.ts injectArticlesSummary`), and never bakes it into
the render cache (`render:v<N>`, `index.ts`). The articles changes independently of `articles.version`, so a cached box
would be stale for up to 30 days. The cached base bytes do not change, so **no render-cache
key bump is needed**, and the legacy annotation rendering is untouched.

The box costs at most two R2 reads per article view when bound, `current.json` (the publish
switch: no box without it) and one index shard, each cached for a minute per isolate, and
nothing when unbound. A store failure is logged and drops the box; it never breaks the page. The match
is by title, because D1 stores no page id, so a page renamed after the store was built loses
its box until the next build.

## 10. Feedback, and how it becomes benchmark seeds

**Table** (`wiki/migrations/0015_atlas_feedback.sql`). The table, its indexes and its view
keep the `atlas_feedback` name the rename of 2026-11-05 gave the rest of this plane up:
migration 0015 is applied in production, renaming a SQLite table needs a migration of its
own, and a migration file is never edited (`CLAUDE.md`, "Site / D1"). Columns: `id,
location_key, pair_key, pageid,
revision, campaign, record_set, relation, verdict ('confirm'|'report'|'withdraw'), note (at
most 500 characters), provenance ('human'), user_id, created_at`. It is append-only: one row
per verdict, never updated. A person's current verdict on one judgment is the newest row for
`(location_key, campaign, user_id)`, which the view `atlas_feedback_latest` gives, and there
a `withdraw` means no verdict. `pair_key` is the producer's revision- and commit-free key: a
store built from a `/2` set carries it on every location and the route copies it into the row;
a `/1` store's rows hold NULL.

A verdict belongs to the campaign the person saw. The page shows a signed-in reader their
current verdicts for the store's campaign only, so a report from one campaign is not shown as
pressed in the next, and a verdict in the next adds a row beside the old one.

**Applying it.** Like the others, by hand after a D1 backup (`CLAUDE.md`, "Site / D1"):
`wrangler d1 execute wikilean --remote --file=migrations/0015_atlas_feedback.sql`. It is
idempotent on its own shape. On a database that still holds the superseded table (one row per
`(location_key, user_id)`, with `updated_at`), `CREATE TABLE IF NOT EXISTS` passes over that
table and the next statement, the `pair_key` index, fails because the column is missing. So
the file stops before it creates anything: no index changes and no view appears. Drop the old
table first.

**`POST /api/articles/feedback`** takes `{location_key, pageid, campaign, verdict, note?}`.
It answers 503 when unbound, 403 for a cross-origin request (`src/security.ts`
`checkOrigin`), 401 without a session, and 403 for the pipeline bearer (these rows are
human labels). It is rate-limited per user on `EDIT_LIMITER` (key `articles:<user>`, 429).
After those checks it reads the body through the shared `readJsonCapped`
(`src/security.ts`) with its own entry `MAX_JSON_BODY_BYTES.articlesFeedback` (4 KiB): a
declared or streamed body past it gets 413 "request body too large (max 4096 bytes)"
before parsing. It answers 400 for a bad field and 404 unless the location is on the
current store article. A campaign other than the store's gets **409** "stale", so a
verdict always names what the person saw. `revision`, `campaign`, `record_set` (the
article's SEAL sha), `relation` and `pair_key` are copied from the store at write time, not
taken from the client. The route refuses the pipeline bearer and the `provenance` CHECK keeps
the rows human labels as a property of the data. Until migration 0015 is applied, the route
answers 503, not 500.

**`GET /api/articles/feedback/export`** is for the pipeline bearer only (admin sessions get
403). It returns every verdict as NDJSON, paged from D1, with `pair_key`, `record_set`,
`relation` and `provenance` on each line. Consumers take the newest row per
`(location_key, campaign, pseudonym)`. Users appear only as
`sha256("atlas-feedback\0" + user_id + salt)[:12]` (the domain is the D1 table's name,
which this rename left alone, so no pseudonym was re-keyed), where the salt is the research
export's (`src/security.ts` `researchExportSalt`: the `RESEARCH_EXPORT_SALT` secret, or
`PIPELINE_TOKEN` until it is set; `docs/SECRETS.md`). The domain prefix keeps these
pseudonyms apart from the research export's `sha256(user_id + salt)[:12]`:
this export carries notes, so a note that names its author must not let anyone join it to
that person's pseudonymous annotation history in the research export. Within this export a
person's pseudonym is stable, so their verdicts still group. The note is the only free
text. The page tells signed-in readers that maintainers read
notes. The export reads only D1, so it keeps working if the store is unbound.

**Seeds.** Proposal §8.1 makes the maintainer's spot checks tier **G4** (up to 30 negatives
and 20 positives per release). Consumer-side human input is never a gold tier of its own.
Feedback rows are G4 **candidates**: a `report` is a candidate negative and a `confirm` a
candidate positive; a `withdraw` takes back that person's earlier verdict on the judgment.
The maintainer adjudicates them before any enters the gold manifest. A row names its sealed
record set and the relation judged, and `location_key` recovers the declaration from that
set; the relation is not part of `location_key`, so a verdict on one campaign does not carry
over to a re-judged relation. Per §8.4, the export stays
on the operator machine and never goes to the fleet host, and val/test seeds never feed
the improvement loop.

## 10a. Community tags: the overlay (release R1)

> **New in release R1** of the signed public-editing plan (2026-11-06; decisions 1–5, 11 and
> 13 at their recommendations). Migration **0019**. The plan's own sections are cited below as
> §2.2, §2.5 and so on.

The plane's second write. A reader who is signed in can **tag a passage** with a Lean
declaration, **change** the relation or note of a location already there — the pipeline's or
another editor's — and say a link is **wrong**, which removes it. Everything is live on save:
no queue, no approval, the same promise `brain_edges` already makes for the Brain.

### Why it is an overlay and not a store write

A set's seal is a statement about bytes, the store is content-addressed, and
`/api/articles/records/:pageid` is checked against `records_sha256` before it is served. So a
community edit **cannot** be written into the store. It is a D1 overlay over an immutable
base, the pattern this codebase already runs for the Brain: live on create, attributed,
human/agent labelled, soft delete leaving a gravestone. The sealed bytes and the cacheable
view model are untouched by every route below.

### The key that makes it work

A `/2` store carries `passage_key` on every passage and `pair_key` on every location:

```
passage_key = sha256("<pageid>\n<span_sha256>")
pair_key    = sha256("<passage_key>\n<repository>\n<declaration>")
```

Both are revision-free and commit-free, so a tag survives a store rebuild, a new universe and
a re-cut — and a community tag of the declaration the pipeline already located on that
passage **is the same row** as the pipeline's location. Agreement, correction and removal all
share one key; there is no matching problem and no fuzzy re-pairing. Note the key names the
*repository*, not the library id (two libraries of one repository share a repository), so the
row carries its library id as a column beside the key. A `/1` store carries neither key and
cannot take an overlay: the tag routes answer **422** on one, and the page offers no form.

### `article_tags` (migration 0019)

One row per `pair_key`, last edit wins, under optimistic concurrency on the row's own
`version` (1 on create, +1 per edit; a write whose `base_version` does not match answers 409
`{error: "stale", version, tag}`). `state` is `live` or `removed`; `origin` is `community`;
`base_relation` holds the pipeline relation a change overrides. `pageid`, `revision`,
`campaign`, `record_set` and `universe_sha256` are copied from the store at write time, never
from the client, and the **repository is derived from the article's own universe**, so a tag
always names what its editor saw. Removing leaves the row with `removed_by`, `removed_at` and
a required `removed_reason`.

The same migration creates `site_settings` (the brake and the annotation plane's editing
switch) and `user_agreements` (the CC0 agreement, below). `users` is not altered by any of
it.

### The routes (§2.2)

```
POST /api/articles/tag                    {pageid, campaign, passage_key, library,
                                           declaration, relation, note?, base_version?,
                                           cc0?, turnstile_token?, model?, run_id?}
POST /api/articles/tag/:pair_key/state    {pageid, campaign, state, reason?, base_version?}
GET  /api/articles/overlay/:pageid        the article's community rows            (no-store)
```

- **Account required** (decision 2). Anonymous readers keep the voice they have: confirm,
  report and the anonymous flag route. The pipeline bearer is refused — it writes the sealed
  store, not the overlay; agents are allowed on these two routes and nowhere else on the
  plane (below).
- `checkOrigin` on every write, and `added_by` comes from `getUser`, never from the body.
- **Validation, all of it local:** `library` must be one of the article's own universe;
  `declaration` at most 240 characters against Lean's name grammar, with **no existence
  check** — the site's autocomplete index is Mathlib-only and a contributor may be tagging
  TauCeti or Lean core, so a declaration absent from it is marked "not found in the index",
  never refused; `relation` is the locate vocabulary verbatim (`exactMatch`, `closeMatch`,
  `broadMatch`, `narrowMatch`, `hasPart`, `relatedMatch`); `note` at most 500 characters;
  `campaign` must equal the store's, else **409 stale**; `passage_key` must be on the current
  store article, else 404. Body cap `MAX_JSON_BODY_BYTES.articlesTag`, 4 KiB, through
  `readJsonCapped`.
- **No Lean source text is ever stored.** A tag holds the declaration's *name* and its
  location, never a pasted signature: the reader renders a signature only from the universe's
  own library data, under that library's licence. The note's 500-character cap and the form's
  own line say so.

### How the reader shows it (§2.8, decision 4)

The rule: **the sealed set is shown as the sealed set, always; the overlay is shown as the
overlay, always; and the default view is both, merged, labelled.**

| Case | What the reader shows |
|---|---|
| Pipeline location, untouched | as before, with its Confirm/Report |
| Pipeline location an editor agreed with | the pipeline card, with a line naming who confirmed it — not a second card |
| Pipeline location changed | the community value, with the pipeline's beneath it: "The pipeline said *more general*; changed by @alice on 7 Nov" |
| Pipeline location removed | folded under "Removed by an editor", struck, with the reason and the remover; one click restores it |
| A community tag at a new pair | a card with a **Community** badge and "Added by" |
| An agent's tag | the same card with the agent's name and "owned by" the person who minted it, both linked |

- The passage's **derived status is recomputed over the merged set**, by the same rules the
  store uses, so a community `exactMatch` on a passage the pipeline left `not_formalized`
  moves the article's headline. **"As published"** (`?overlay=off`) shows the sealed statuses
  and the sealed cards; the merged view is the default, because otherwise an edit has no
  visible effect and nobody makes a second one.
- A community removal of one part of a survived coverage claim withdraws the claim.
- **Caching, and the trap it avoids.** `/api/articles/article/:pageid` stays the sealed view
  model verbatim, `public, max-age=300`, cacheable and hash-checkable: the overlay is **not**
  merged into it. The merge happens server-side, per request, on pages that send no
  `Cache-Control`, so **no `render:vN` bump is needed for any of this**.
- Jack's display rule holds on a community card exactly as on a pipeline one: the Lean code
  leads, provenance sits behind a closed Details fold, and no voting data is shown. A
  community card carries no Confirm/Report: those are a verdict on a *sealed* location, and
  their `provenance` CHECK keeps that column honest.

### Abuse controls (§2.5, §2.6, decision 3)

| Control | Where |
|---|---|
| Account required | every write route |
| Burst, established editor | `TAG_LIMITER`, 60/min, key `tag:<user.id>` |
| Burst, newcomer | `TAG_NEW_LIMITER`, 6/min, key `tagnew:<user.id>` |
| Burst, an agent | PR #126's one 30/min key per **owner** (`EDIT_LIMITER`, `agents-of:<owner>`), shared with its saves and Brain writes |
| Daily volume | computed at write time: `new` 50/day, `autoconfirmed` 500/day |
| Bot-shaped signup | Turnstile on a new account's **first** edit only, never after |
| Site-wide write brake | `site_settings.site_write_mode`, read per write |

A **newcomer** in R1 is an account under four days old **or** with fewer than ten overlay
edits — §2.6's rule minus its revert-ratio term, which needs reverts, which arrive in R2.
There is no `user_trust` table yet (migration 0022, release R3); the tier is derived from the
account and its rows in one query. An agent takes its **owner's** tier and cap and never
exceeds it.

**The brake is not optional.** `wiki/src` ships only in a new release — a fresh P1B bundle
and Jack's approval, about 5.5 hours — and there is no Worker-only hotfix path (the ROADMAP's
"Waiting on Jack" item 8). Opening public editing without a lever would make an abuse bug a
5.5-hour exposure. The brake is the lever, and it is one statement:

```sql
UPDATE site_settings SET value = 'locked', updated_at = <now in ms> WHERE key = 'site_write_mode';
-- and 'open' to reopen
```

It covers both tag routes, the feedback route and **every agent write on the site**, Brain
writes included. It is read per write, `open` allows, anything else refuses with 503 — fail
closed, because an unknown value must not be a hole. **This brake is the documented
substitute for a Worker-only hotfix path** until Jack defines one (decision 3); it does not
replace one for a bug outside these surfaces.

Deliberately **not** in R1 (they are R2 and R3): history pages, diffs, revert, watchlists,
page protection, trust tiers, the patrol queue, the per-article flood cap and the
open-dispute cap, and the user-scoped mass revert. R1's correction model is the Brain
overlay's: live on create, soft delete with a gravestone, and an admin reverts by writing the
row back.

### The CC0 agreement at a first edit (decision 11)

The mapping is CC0 and a waiver needs consent, so the first time a person writes to the
overlay they are shown a checkbox and the write is refused (403 `{cc0_required: true,
cc0_version, cc0_text}`) until they tick it. The acceptance is one row in `user_agreements`
with its **timestamp** and the wording's version id, and it is never asked for again. The
wording is in `wiki/src/articles-overlay.ts` (`CC0_AGREEMENT_TEXT`); changing it in a way
that needs re-acceptance is a version bump.

**An agent cannot agree to a licence; its owner can, and must have.** An agent's write is
refused until the person who minted it has accepted, which is the second reason every agent
names a human.

Turnstile rides with that same first edit. The **site key** is public configuration
(`wrangler.jsonc` vars, `TURNSTILE_SITE_KEY`); the **secret** is a Worker secret,
`TURNSTILE_SECRET`, listed by name in `docs/SECRETS.md` and never written down anywhere. With
no site key no widget is rendered; with no secret the check is skipped and logged, so a
deployment the maintainer has not yet given keys still takes edits under the other controls.
A page that renders the widget — and only such a page — widens `articlesCsp` by
`https://challenges.cloudflare.com` in `script-src`, `frame-src` and `connect-src`.

### Agents on the overlay (decision 13, §2.7)

PR #126's identity model is taken as given: an agent is its own `users` row, `role='agent'`,
named "<name> (agent)", with an `owner_id`; only the token's SHA-256 is stored; a person has
at most 5 live agents and 25 minted ever. R1 extends it to **the two tag routes and nothing
else of this plane**:

- `POST /api/articles/feedback` stays refused. Confirm and report are a human reader's
  verdict and a `provenance` CHECK keeps the column honest; an agent that wants to say the
  same thing writes an overlay row, where it is labelled and revertable.
- An agent's rows are `actor_type = 'agent'`, `via = 'api'`, and may carry its declared
  `model` and `run_id` for the research export. `base_version` is **required** for a bearer
  write.
- **An agent may not change or remove a tag a person wrote** (403). §2.7's design for that
  case — relabel it and keep the person's version in history — needs history, which is R2's.
  Until then the conservative answer is a refusal. A person may correct an agent's tag
  freely.
- The authorization matrix (`wiki/test/authz.test.ts`) carries the `agent` column for all
  three routes; it is the artefact that makes this safe to change.

### What is not here

No history, diff, revert, watchlist, patrol queue or research export for the overlay — all
R2 and R6. No anonymous suggestion queue (R6). No page protection or trust-tier table (R3).
The actor vocabulary is **not** unified across the four spellings the codebase uses
(`human`/`ai`, `human`/`pipeline`, `kind='agent'`, and the overlay's `human`/`agent`): that is
decision 7, needed by R4, and it is not in R1's signed scope. The overlay's own tables use
`human`/`agent`, which is where decision 7 recommends the rest should land.

## 11. Release coupling

| Path | Class | Ships |
|---|---|---|
| `docs/ARTICLES.md`, `scripts/articles/**` (builder, fixtures, test), `wiki/test/**`, `wiki/e2e/**` | release-neutral | on merge |
| `wiki/src/articles.ts`, `articles-render.ts`, `articles-math.ts`, `articles-overlay.ts`, `site-settings.ts`, `libraries.ts`, `security.ts` (`articlesCsp`), `env.ts`, `index.ts`, `pages.ts`, `wiki/wrangler.jsonc` | release-affecting | only in a new release (fresh P1B, Jack's approval) |
| `site/assets/articles.{css,js}`, `site/assets/articles-reader.{css,js}`, `wiki/scripts/build-public.ts`, `wiki/public-asset-source-attestation.json` | release-affecting | with that release's public baseline |
| `wiki/migrations/0015_atlas_feedback.sql` | operator action | applied to D1 after a backup; fail-soft until then; stops on the superseded table (section 10) |
| `wiki/migrations/0016_articles_wikipedia_title.sql` | release-affecting file, optional operator action | ships with the release like any file under `wiki/migrations`; nothing requires it on D1, so the operator may apply it at any time after a backup, before, with or after that release (section 7) |
| `wiki/migrations/0019_article_tags.sql` | operator action, **before** the release | the promoter refuses any Worker built from a commit containing it until `article_tags` is on D1 (`D1_REQUIRED_COLUMNS`). PR #126's `0017` and `0018` come first, for the same reason |

- `articles.css`, `articles.js`, `articles-reader.css` and `articles-reader.js` are copied by
  build-public but are not in the required baseline lists, so baselines frozen before the
  plane existed stay valid. The next baseline freeze includes them, against the source attestation,
  which names each with its sha256 and bytes and is regenerated in the change that ships it.
- build-public still deletes a stale `wiki/public/atlas.html`, and baselines still forbid
  one; the rename adds `articles.html` to both, and deletes the reader's assets under their
  old `atlas*` names. That matters: assets are served before the Worker, so a leftover
  `articles.html` would shadow `/articles` and a leftover `atlas.css` would be served stale.
- The release that carries the binding (section 6) shows no data until `current.json`
  is uploaded: until then `/articles` answers 503 "articles store has no published build" and
  article pages show no box. A build without the binding answers 503 "articles store not
  configured" on every route of the plane instead, and nothing outside it changes.
- The same release links `/articles` from every page's navigation (section 14, item 8), so until
  the upload, or after `current.json` is deleted, that link leads to the 503 page.

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

The articles plane never writes the Brain. Locate output reaches it only (a) as
`brain/proposals/*.jsonl` through the offline fold, or (b) after Jack chooses a release-gate
mode for content changes. Concept edges come only from `subject` passages whose own QID was
validated against a sealed Wikidata bundle, and each library maps to one `brain_lib` token.
A token names one repository, and a repository may carry several (lean-core split into
`Init` and `Std`, contract note 13). Non-Mathlib libraries stay mentions-only by current
policy.

## 14. Prerequisites and decisions

Jack decided most of these on 2026-11-02; each says so.
Two wait on his ratification before batch 1's store is uploaded, and one is still open, with a
recommendation.

1. **The End-goal amendment** (`docs/ROADMAP.md`, "End goal"). *Ratified (Jack,
   2026-11-05).* Everything here assumes it.
2. **Store backend.** *Decided (Jack, 2026-11-02): R2, one private bucket* (no public URL, no
   custom domain), bound as `ARTICLES` in `wiki/wrangler.jsonc` (section 6). *Proposed, for Jack to
   confirm:* the bucket's name, `wikilean-articles`, which the binding gives. The maintainer creates
   the bucket under that name before the release that carries the binding is deployed; a missing
   bucket shows only at that deploy. Data updates without a Worker release, there is no
   per-version asset-count limit, and the free tier is ample. The alternatives were KV (on the
   free plan its 1,000 writes a day cannot take a ~40k-object upload) and static assets (every
   data update coupled to the ~5.5 h release chain, and the asset-count limit of about 20,000
   files on the free plan at full scale).
3. **Who uploads, and from where.** *Decided (Jack, 2026-11-02):* the maintainer, from the
   operator Mac, with a token scoped to the one bucket (Object Read & Write, with an expiry,
   in `docs/SECRETS.md`'s register); never the fleet host, the nightly or GitHub Actions. The
   upload follows section 6, through `scripts/articles/upload_store.py`, which takes the
   token's S3 credentials from the environment or a 0600 file, never from the command line
   (`scripts/articles/UPLOAD.md`).
4. **Public licence statement.** *Decided (Jack, 2026-11-02):* the reader says the page is an
   adaptation of the pinned revision, shared under CC BY-SA 4.0, with the article's title, a
   link to the revision, its history for the contributors, the licence link and the list of
   changes (the highlights, the cards and links added, the images removed, the navigation boxes
   hidden); images and media keep their own licences; the mapping (which passage is formalized
   where) is CC0, distinct from the page; each article's JSON names both (`licence`, section 4).
   The Lean code on the cards is quoted from its libraries and keeps each library's own
   licence, which the universe page lists; the CC0 release does not cover it. The Wikimedia
   and Foundation marks and any image that is not free are left out, and the builder refuses
   them (section 5); nothing under `/articles` is indexed (section 7). To meet the images' own
   licences, the reader lists each image and media file with its file page, its author wherever
   its licence asks for the author's name, and its licence, linked to the licence's text where
   it has one (section 8, "Image credits"), as CC BY and BY-SA before 4.0 and the GNU licences
   ask on every copy.
5. **Feedback export carries notes.** It is the one free-text field, unlike the research
   export, and its pseudonyms are domain-separated from that export's (section 10) so the
   two cannot be joined. *Open; recommendation:* keep the notes, because reasons make reports
   usable, and the form says maintainers read notes.
6. **Absence feedback.** Readers cannot yet say "this is formalized, here" against a "not
   found". That is the safety metric's most valuable label (false-not-formalized, §8.2). It
   needs a passage-level key, for example `(record id, universe sha)`. *Decided (Jack,
   2026-11-02): launch without it.* *Recommendation:* build it next, as a second verdict target.
7. **The producer contract notes** (section 3). *Agreed (Jack, as formal-companion's
   maintainer, 2026-11-05).* Notes 1, 2, 8, 9, 12 and 13 describe what
   the producer's export at master writes; the others are WikiLean's readings where the
   agreed format (note 10) left a choice.
8. **Navigation.** *Decided (Jack, 2026-11-02): link the plane from the site's navigation in
   the release that binds the store.* Done with the binding, and then absorbed by the rename
   (section 0): the header of every Worker page that carries the site's navigation (article
   pages, `/`, `/about`, the pages of the `pages.ts` shell such as `/stats`,
   `/recent-changes` and an article's history, and the two Wikifunctions pages) links
   "Articles" before the Brain, as the plane's own header does. On the shells that already
   had an "Articles" link that link simply leads here now; the two Wikifunctions pages gain
   one. The render cache key and `page:home`, `page:about`, `page:stats`,
   `page:wikifunctions` and `page:wikifunctions-verify` moved with PR #125 and stay moved,
   conservatively: a reader following that link now lands somewhere else.
   `page:articles` is retired, not moved, with the directory it cached. Pages with a
   navigation of their own keep it:
   `/brain/api` and `/mcp` (the agent API's), `/repos` and the bulk-connections page
   (`/quickstatements`). `/brain` comes from the frozen Brain release, and the 404 and Concepts
   pages from the public baseline, so this change leaves them as they are.
9. **The notice.** *Decided (Jack, 2026-11-02):* one line under the header of every page of the plane,
   "Preliminary: these links have not yet had an independent audit.", never a badge on a card.
   The figures an audit measured go in the "About this data" fold, from the store (section 8,
   "Accuracy").
10. **Audit before upload.** *Decided (Jack, 2026-11-02):* a blind audit of a sample of batch
    1's links runs before its store is uploaded. Its figures reach the pages through the store's
    audit summary (`--audit-summary`, section 5), so they can change without a release.
11. **Passages with no status.** *Decided (Jack, 2026-11-02):* no highlight and no status until
    they are read (section 8).
12. **Reader reports.** *Decided (Jack, 2026-11-02):* the maintainer reviews them weekly, from
    the feedback export on the operator Mac (section 10), before any becomes a benchmark seed.

## 15. Tests

- `scripts/articles/test_build_store.py` (in `scripts/ci-python.sh`) checks fixture
  reproducibility, schema validity, the golden store, determinism, the manifest, the view
  model (including each passage's basis, the per-library proof split and the article's
  `licence`, its records' one subject licence, two of them refused), `quote_capped`
  and `passage_ends_in_suffix` (a 12-word head and a 12-word whole passage in the fixture, a
  quote under the cap, a span that ends in whitespace, a passage that ends inside its quote's
  suffix or goes on past it, hostile `span_sha256` values given to `quote_extent`, F5's
  span as a shortcut that gives the hash's answer in every case and settles alone where its
  tail allows, the fixture with every passage's span building its golden articles, and
  records of one passage that disagree on `span_sha256`, in either order), the counts by
  prominence per article and in `current.json`, title shards,
  the §2.6 recomputation in both directions, its parity with the producer's truth table
  (`producer_derive_status.json`, coverage cases included), with the hand-written table
  both sides run (`status_parity.json`, pinned by sha256, every case through
  `derive_passage_status`, refusals included, and that function against `build()` on every
  fixture passage) and the §1.11 cases,
  decision D1 (exact and close matches to data, broadMatch to data, data on sorry, and the
  coverage claim: survived against every other challenge value, the round-three reading's
  part cases, finished and unfinished, data and Prop-valued definitions among them, on
  every passage kind, the parts' challenge values a survived claim confirms and the ones
  it leaves, the basis and ties, the parts' order, and every refusal of the claim's shape
  and parts), the `statement` flag, related-only
  passages, the agreed record-set format (the seal's exact keys, campaign ids and run
  tokens, the token grammar of `set_id` and `sourceset.id`, `byte_budget` kept and
  enforced, the universe's sorted list and exact library and exclusion keys, the `repo`
  grammar, the length caps, a null licence, the pre-agreement shapes refused by name,
  `section` and `prominence` on every record, every challenge value with
  `challenge_pending` on a location, an absence and an unresolved record, receipt counts
  as list lengths, required `module`, `path` and `line`, optional but never null
  `end_line`, closeMatch notes, minimal absence ends, `universe.yaml` refused), several
  libraries per repository (`init` and `std` at one lean4 commit beside `mathlib` building,
  a location in `std` and a related declaration in `init` kept apart by id through the view
  model, the counts and the index;
  an absence naming lean4 once building, twice refused, and one repository at two commits
  needing two ends; a repeated library id, a root of one repository in two libraries at one
  commit or two, and a root listed twice, each refused; one root name in two repositories
  building), the
  locate extension's keys (an unknown key at each of its places refused with its place
  and the record id, a key of another shape, a stray receipt key, another extension
  namespace on the record and on either end, the subject end's `renderer`, `licence`,
  `span_sha256` and integer `pageid`, integer receipt counts), F5's optional `span` and
  `layer_sha256` (a valid pair building to the view model the same set builds without it,
  the records keeping them;
  a malformed span, a malformed or foreign layer sha, either one alone, a span shorter than
  its quote, a `span_sha256` the quote contradicts, and records of one passage that
  disagree, each refused), the other refusal cases (oldid, title
  keys, templates, dot-segment repos, display names, renderer, unknown statuses,
  non-string affixes), hostile values in some 90 fields of the records, sources, universe
  and seal (refused, never a crash), the CLI (exit codes 2 and 3, `--replace` refusing
  foreign content, and any interpreter but CPython 3.12.13 or a later 3.12 refused, exit 3,
  before anything is read), and the four optional side files (`SupplementTests`: a build
  without them byte for byte the golden store; each merged where it checks out, with the
  rest of the tree unchanged; a passage text whose layer, id, span sha or segments are off, a
  signature that is another declaration's or of another universe, and provenance that does not
  describe the set, its runs or its record lines, or whose runs' windows do not follow the seal
  and one another (a harvest after the export, a challenge that starts before the run it
  re-checked was harvested, an inventory harvested after its locate run started, times compared
  as times), each refused by name; the inventory run a locate run names cited and checked as the
  other runs are; an audit summary cited by every article and by `current.json` with the rest of
  the tree the plain build's, and refused when it names another build's record sets, an unknown
  key, a malformed run, window (or one that starts before the latest seal's export), model or
  figure (a share outside its interval or outside 0 to 1, a draw outside the sample, a population
  smaller than its draw, an effective sample above its draw or, without weights, other than it);
  hostile values in some 70 of their fields refused, never a crash; `--check` with and without
  them), and the reader
  side file (`ReaderHtmlTests`, over a synthetic body of the Widget lemma: stored byte for byte
  where it checks out, the rest of the tree the plain build's; an unmarked passage unplaced and
  required to be listed; a mark on a passage with no status refused, whatever status it claims; a changed character, a mark one character late or early, an unmarked
  piece between two marks, a wrong status, an unknown key, nesting, a mark across an element
  boundary, a missing or extra anchor id, some twenty things a page must not serve and an entry
  for another revision, each refused by name; the image rules (a Commons file, an English Wikipedia
  upload with a free licence, a video and its poster and a TemplateStyles `url()` stored and
  listed with their licences; an upload with no licence named, a non-free one, a file listed
  under the wrong project or not shown, a URL that names no file, the Wikimedia and Foundation
  marks however listed and whether or not "logo" is in the name, a file without its restrictions
  or with malformed ones, a trademarked file whatever its name, a Commons file with no licence or
  with an id-less one not free by its name, a file under a licence that asks for its author's
  name without one, and one whose licence has a text of its own without an https link to it on
  the licences' hosts, each refused, while a name that only resembles a project's, a restriction
  other than `trademarked` and a public-domain or CC0 file without an author build); attributes
  outside `READER_ATTRS`
  (`attributionsrc`, `browsingtopics`, `lowsrc`, an entity-named one and the like), articles links
  outside the slug grammar or with a dot segment (`..`, `%2e%2e`, entity-spelled), links to the
  site's own host however spelled, the page's own classes, and further hostile cases by name
  (meta, base, svg, link, button, obfuscated `javascript:`, `data:` URLs, a MathML `href` and the
  MathML style variants); a URL attribute (`href`, `cite`, `src`, `srcset`, `poster`) holding a
  space character a browser keeps in the URL (U+00A0, U+3000, U+2009, U+0085) and a TemplateStyles
  `url()` padded with one, each refused, while ASCII whitespace still splits a srcset and pads a
  `url()`; an English Wikipedia upload without its categories, with malformed ones, outside
  `Category:All free media` or in one that makes it not free everywhere (a made-up photo with the
  API record's shape for one free in the United States only, under freedom of panorama, among
  them), each refused; a
  passage's context that is not the proven layer's text beside its span, one word off or from
  elsewhere in the article, refused with the reader html and built without it; `--check` with
  and without it).
- `wiki/test/articles.test.ts` (in `npm run test:ci`) checks 503 when unbound, `current.json` as
  the publish switch (without it every route but the export a 503 and no article box, the bucket
  read once; its absence and its return each shown within `INDEX_CACHE_MS`), the APIs and
  pages on the fixture store, 404s, escaping and integer-only numbers, the headings, the
  visible status sentence and its basis, the store-object cache (each small object read once a
  minute whatever the route; a sha or shard `current.json` does not name a 404 that reads
  nothing; a small object that does not parse kept as that failure for the minute, so the
  dashboard and the universe page read it once), damaged store objects (a truncated article,
  in its pages and its two JSON views, records cut short or missing, a title no URL can carry
  in an article or an index shard, one without its universe, a truncated or foreign
  `current.json`, a universe of another shape: the plane's 503 page or JSON, logged, never a
  500; an article whose lists are not lists rendered without them), the search limiter
  with its IPv6 /64 key, the meta budget (its key, 429 over it, the dashboard and the universe
  page taking none) and the article read budget (its routes, its key apart from search's, a 429 with
  `Retry-After` before any store or D1 read, saying nothing of how it is kept), migration 0016's
  query plan (the slug lookup a search of the index, a re-run a no-op), the slug lookup's
  per-isolate memo (one D1 read for repeated views, with or without a WikiLean article, read
  again after a minute, at most `MAX_SLUG_CACHE` titles, per binding, no failure kept),
  `X-Robots-Tag: noindex` on every answer under `/articles` and `/api/articles/` (pages, JSON, 404,
  429, 503 and the site's 404 there), on none outside and never on `/api/articles` itself, and
  each page's robots meta, the
  footers' licence statements (the reader an adaptation of its revision under the licence the
  article's JSON gives its text, with what changed, `rel="license"` once, and its images and media
  listed with their file pages, authors and licences, each licence linked over https only, a
  malformed entry left out and a name escaped; the
  card list crediting its revision; the mapping's CC0 on every page), the notice
  (once under the header of every page of the plane, 404, 429 and 503 ones included, at the top of the
  reader's article column, and never in the article box or on a card) and the "About" folds'
  accuracy lines (the figures as estimates and the audit run from an audit summary, a malformed
  one read as none and escaped, else that the build carries no accuracy figure), the "About"
  folds' runs (the inventory run first when the store names one), the dashboard's date (its
  build's, or the record sets' export when the build took that as its time) and its passage-text
  count (of the passages it shows), feedback
  auth, origin, validation, size cap (the shared `readJsonCapped` cap, exact to the byte),
  rate limit and missing table, append-only rows and withdrawal, what a row copies from the
  store, a verdict not pressed in the next campaign and not overwritten by its verdict,
  migration 0015 on an empty database, on a re-run and on the superseded table, the export
  and its domain-separated pseudonyms and their
  `RESEARCH_EXPORT_SALT` salt, the security headers on every response of the plane and the pages'
  CSP fit (same-origin loads only, no inline handlers; `articlesCsp` on every page of the plane, its two
  inline-script hashes recomputed from each page, its media hosts the builder's `READER_FETCH`,
  and every HTML answer through `articlesHtml`), the article box with its cache
  isolation and revision note, the readers, non-array universe lists, passages with no status set
  aside in both views (no block, row, section, mark or status label; the counts, the bar, the
  chips and a library's related-only chip without them; the Coverage line's count of those left
  out), Prop-valued-definition passages, the subject passage's marking and its Subject row, a
  `challenge_pending` location ("not challenged yet"), the coverage claim's line ("Formalized
  by 2 parts together" with the parts listed, only for a survived claim; its wording for a
  statement-only claim and for one an older store gives no status; a malformed claim never
  reaching the HTML), the theorem a data declaration formalizes, a location without `end_line`
  ("line 88", `#L88`), a run-token campaign on the pages and
  in feedback (`CAMPAIGN_RE`, the same grammar as the builder's), the producer's truth
  table and the shared parity table's claim-free cases for the page's fallback rows, a
  capped quote's ellipsis outside the quotation (in the card, after any suffix, and after
  the Subject row's closing quotation mark; none on a 12-word whole passage, none on the card
  of a head whose passage ends in its suffix, and none of a suffix's own; the word count
  for a store without the flag), the central-statements line on the article page and the
  dashboard (its place after the bar's legend, its wording for every count, and none for a
  store without prominences or `counts.by_prominence`), the by-library order and the list of
  empty libraries after the column notes, the rows a dashboard without its universe object
  takes from the counts, the heading's hidden separators, and
  the library registry (seeded names, look-alike
  display names, ignored templates, repository-relative paths, dot segments,
  GitHub-only repository links, and libraries that would read alike named apart by their
  roots or ids), and libraries split from one repository shown per id (Lean core · Init and
  Lean core · Std on the universe page, the coverage page's chips, cards and status
  sentence, and the dashboard's table, library names and article list), and the article
  reader ("the article reader", over the golden store plus the Widget lemma's synthetic marked
  revision, `test/helpers/articles.ts articlesReaderFiles`): the revision served verbatim, a hidden
  panel section per passage holding exactly the card list's cards with Details closed, the
  reading order and section groups, an unplaced passage, `?view=cards` with the switch and
  "Show in article", the card list reading no revision (offering the reader from the view's
  sha256 alone, the reader then the card list when the file is gone), a store without the file
  or with other bytes serving the card list, the same-origin loads and an `articles-reader.js` that
  fetches and parses nothing, the article's paint containment, and the security headers; the
  policy's script files by URL and never `'self'` (every script one of its pages loads among them),
  its one form (`/articles`) and the origin it names, and the site stylesheet at the article pages'
  `?v=`. `wiki/test/security-headers.test.ts` covers the two
  reader assets' headers, and a route's own enforced policy kept with the framing ban beside it:
  `articlesCsp` on the reader and every other page of the plane (the 429 and 503 ones too), its
  directives, and the shared set alone on the plane's JSON and every other page.
- `wiki/test/articles-binding.test.ts` (in `npm run test:ci`) checks that `wrangler.jsonc` binds
  `ARTICLES`, and nothing else, to the bucket `wikilean-articles`, and that the Worker reads the store
  through the binding of the name the config gives. Without the binding, every GET route the app
  registers answers the status it answers with it, except the plane's own, which answer 503
  "articles store not configured"; the pages outside the plane keep their bytes, and only an
  article page reads the bucket (the publish switch and its title's index shard); the feedback
  export, which reads only D1, keeps answering.
- `wiki/test/site-nav.test.ts` (in `npm run test:ci`) checks the navigation's one `/articles`
  link in the header of a page of each shell that carries the site's navigation, before the
  Brain where that is listed and with no `/atlas` link left anywhere; the moved cache keys,
  with the link in their cached bytes and nothing left under the old ones; that nothing writes
  the retired `page:articles` key; and that `/api/articles` keeps its key while the sitemap
  drops `/articles`, which now asks not to be indexed.
- `wiki/test/articles-redirects.test.ts` (in `npm run test:ci`) checks that every URL the
  reader answered under `/atlas` and `/api/atlas/<sub>` 301s to its new path, query string and
  all, with or without the binding and reading nothing from the store; that the old graph
  stack's bare `/api/atlas` and `/atlas_data.json` keep their plain 404; and that
  `/api/articles` answers exactly as it did, with no `X-Robots-Tag`.
- `wiki/test/retired-routes.test.ts` keeps `/atlas` out of the deleted paths (it redirects now),
  but still checks that `atlas` is reserved, so no article can be created under it.
- `wiki/e2e/articles.spec.ts` (the required browser job since PR #72) runs axe in both colour
  schemes at desktop width and at 375px (the Gadget page with its coverage claim's parts
  list included), plus the parts list itself, a passage heading's accessible name, a capped
  quote's ellipsis outside its highlight, the central-statements lines, the by-library
  order with the empty libraries' list opened at 375px (axe, no sideways scroll), the
  combined filters, feedback, live search,
  a passage permalink, the Subject row's link, the article box and the site header's Articles
  link (from an old article page to the reader's list, which alone marks it as the current page, and
  the article page's header at 375px with no sideways scroll), and runs those flows
  again under the plane's enforced policy as served (the header search among them) and the
  article box under the Worker's Report-Only CSP enforced, asserting no violation. Its "the
  article reader" tests run on
  the harness's third listener (port + 2, the store with the Widget lemma's marked revision):
  axe in both colour schemes with and without a passage selected, a click opening the passage's
  cards, the stepper in reading order and "← All passages", the keyboard (one stop per passage,
  Enter, ← →, Esc returning focus), a `#p-` link and an unknown one, the hover peek, a status
  filter, a link to a passage with no status finding none, the notice atop the article column
  with the panel still filling the window under the header, the bottom sheet at 375px with no
  sideways scroll, the reader under its enforced
  policy (the theme bootstrap, the header search and `articles-reader.js` with no violation; an
  inline script, an inline handler and another same-origin script file it did not ship refused
  and reported), an overlay of the
  highest z-index inside the revision covering neither the header, the panel, the page head nor
  the footer (and on a phone neither the header nor the panel's button), the switch to the card
  list and back, and Confirm from the panel.
- `wiki/test/request-limits.test.ts` puts the feedback route in the shared table of JSON
  body caps and scans `wiki/src` so that no route reads a JSON body except through
  `readJsonCapped` with its own `MAX_JSON_BODY_BYTES` entry.
- `scripts/articles/test_upload_store.py` (in `scripts/ci-python.sh`) runs the uploader against an
  in-process fake S3 endpoint. The endpoint refuses any request whose SigV4 signature
  (recomputed from the request as received), payload hash or Content-MD5 does not match, and
  the signer is pinned to AWS's documented examples. The tests check: `current.json` after
  every other object and the deletes after it; a dry run that writes nothing; no credential in
  any output or in the builder's environment; refusal when `--check` fails or exits 0 without
  its report; a wrong ETag, bytes that change after upload, a lost object and a delete that did
  nothing; retries; deletes confined to `articles/v1/`, with keys a server could read as other
  keys refused; a listing that returns keys outside `articles/v1/`, whether it plans the run,
  feeds the deletes or is the final check's, never acted on, and one that does not say its keys
  are URL-encoded refused when a key reads differently decoded; what each failure reports
  (nothing written, or what the bucket holds and what readers see, on a first publish and over a
  live build), and a re-run converging after each; `--unpublish`, `current.json` first; and a
  rollback, with `--store` refusing a build older than the live one, `--rollback-to` one newer,
  and the rollback command a publish prints working as printed.
