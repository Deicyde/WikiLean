# The formalization atlas

**Status (2026-10-18): built on branch `claude/formalization-atlas`, not deployed.** The
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
- **A location's lean end** carries `location.declaration` and `location.module`, and in
  its locate extension `library`, `path` (relative to the repository root: Lean core's
  files are `src/Init/...`), `line`, and `end_line` when the library index records one.
  All of these are required except `end_line`: a lean end without a module, path or line
  is refused, so the producer should refuse such a passage itself, by name, rather than
  export it. atlas's published index records only `line`, so `end_line` is then
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
- **An absence keeps one lean end per universe library**, each minimal:
  `{"type": "lean", "uri": <repo>, "version": <40 hex>}` and nothing else (no `role`, no
  `extensions`, no `location`). On smoke-2's real set that shrinks an absence from about
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

### What the builder checks

`scripts/atlas/build_store.py` refuses the whole set, listing every problem it found, when
any of these fails:

1. **Seal.** `SEAL.json` has exactly the keys above, with the token grammar for `set_id`,
   `universe.id` and `sourceset.id`, the campaign token grammar and a `byte_budget` that
   is an integer 0 or more; every listed file exists and matches its
   sha256; no unlisted file, symlink or unsafe path is present. Receipts are named by
   their own sha256, and every receipt is referenced. Hex, token and time values must
   match in full (a trailing newline does not pass).
2. **Universe.** `universe.json` has exactly the keys above (a `universe.yaml` is refused);
   its id and `universe_sha256` equal the seal's; `libraries` is a list sorted by id, and
   repositories are unique; each repository follows the `repo` grammar above (an https URL
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
   does not pass for revision 2002), and its `version` is the revision. Its `pageid` and
   `html_sha256` match `sources.jsonl`, and its quote is within the export caps (12 words
   of `exact`, 32 characters of prefix and of suffix, all strings).
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
     `{type, uri, version}` lean end per universe library, and references its receipt as
     `receipt: {sha256, queries, candidates}`. Each count must equal the length of that
     list in the receipt file, repeats included, and the receipt must list at least 3
     distinct queries (gate rule 5).
   - An **unresolved** passage is a single record with `object: []`, no status and no
     relation.
   - A **coverage claim**, when present, has exactly `complete` (`true`), `parts` (a list of
     at least 2 distinct 64-hex location keys) and `challenge` (a challenge value above);
     `null`, a missing or extra key, `complete: false`, one part, a repeated part, a null
     `challenge` (an owed claim) or a value such as `refuted` is refused, naming the rule.
6. **Passages (proposal §2.6).** The records of one passage agree on kind, quote, section,
   prominence, coverage claim and status.
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
   as outcome `unresolved`, counted as undecided (never as not formalized), with its
   related declarations kept. Such a passage whose records stamp a status is refused. The
   location that justifies the status, the highest-ranked one whose own status is the
   passage's, is stored as the passage's `basis`, so the page names it rather than
   whichever location ranks first. When only the coverage claim reaches the passage's
   status, the basis is null and the claim's own `basis` is true; on a tie the location
   is preferred. `scripts/atlas/fixtures/producer_derive_status.json` (WikiLean's parity
   table) holds the producer's answers for every one-location case, 300 sampled two- and
   three-location cases, and 959 coverage cases (every kind with every pair of parts under
   a survived claim, the pairs of four representative parts, two finished and two
   unfinished, under each other challenge value, and 200 sampled claims with three parts
   or with other locations beside the parts). It is generated by `make_producer_table.py`
   from formal-companion at `4fc2aa0` (branch `locate/d1`, which implements D1 as parity
   round three reads it, with review four's reopen ordering; not yet on its master), and
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
   `scripts/atlas/fixtures/status_parity.json` is the table both sides run: formal-companion's
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

These are where the builder departs from, or adds to, the proposal as written. The
record-set format itself (above: the seal, the universe's list shape, the campaign token,
the byte budget, optional `end_line`, `section` and `prominence`, closeMatch notes and
minimal absence ends) is agreed with the skill (note 10); the rest still needs its
agreement before real record sets arrive:

1. **CSL citations need an `id`.** The vendored schema requires `citation.id` and
   `citation.type`. Proposal §2.7's example citation has no `id` and fails validation as
   written. The fixture uses the source id.
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
   rules, as `cflib.vocab.derive_status` implements them at formal-companion `4fc2aa0`,
   with the maintainer's decision D1 (2026-10-18) as parity round three reads it (note
   11). That code is not yet on
   formal-companion's master, and `references/vocabulary.md` §1.11 marks its other
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
   recomputes as `partial` and the set is refused, so the export must carry it.
9. **A passage with only `relatedMatch` locations** (no checked absence) has no derived
   status, and the producer's export for it is not built yet. WikiLean reads it as
   `relatedMatch` location records without a `status`, which it stores as an unresolved
   passage that keeps its related declarations. An unresolved record (`object: []`)
   beside them is refused, as is a status on them.
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
    refused only after every run has been paid for. Any other key is refused, so a change
    to the format is a change on both sides at once.
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

## 4. The store (v1)

```
atlas/v1/current.json                    campaign, universe summary, record set (seal sha, set_id, byte_budget), totals, build time, index shard list
atlas/v1/manifest.json                   every other file: bytes and sha256
atlas/v1/universes/<universe-sha>.json   libraries by id (id, brain_lib, display name, repo, commit, toolchain, roots, licence SPDX id), exclusions
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
  `location_key` of the location that justifies the status, or null), `coverage` (the
  coverage claim, below, or null), and `locations`,
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
hostile values (null, a bool, numbers, a string, lists, objects) into some 75 fields of
the fixture in turn and requires a refusal or a build, never another exception. The tree
is staged next to `--out` and moved into place, and the previous store is moved back if
that fails, so a failed build leaves nothing half-written. `--out` is the store directory
itself: `--replace` refuses a directory that holds anything besides `atlas/v1/`, so
pointing it one level too high (at `wiki/public`, say) cannot delete unrelated files. `--built-at <ISO-Z>` overrides the
build time (pass the same value to `--check`). `scripts/` is release-neutral, so the
builder can change without a release.

The fixture lives in `scripts/atlas/fixtures/`. `make_fixture.py` writes `recordset/` (two
synthetic articles that cover every outcome, in exactly the agreed format: a lead
passage, two subject passages, a location without `end_line`, a closeMatch from a
challenge downgrade, a `challenge_pending` location) and `store/` is its built store. The Python
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
   metadata beside them (`display_name`, roots, toolchain, licence, exclusions), so the object is
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
  only related declarations is labelled "related only". When the article has subject
  passages (prominence `subject`), a "Subject" row names each one (its kind and quote,
  linking its `#p-<hex>` block) with its status pill. The meta line names the campaign,
  or for a run outside any campaign, the runs ("run L with challenge run C, outside any
  campaign").
- **Passage block.** Under a "Passages" heading, one `<h3>` per passage with its kind,
  prominence and section path, beside a status pill (icon and word). A subject passage
  carries a "Subject of the article" tag in place of the prominence word, the class
  `is-subject` (an accent frame) and `data-prominence="subject"`. A located passage
  then says in visible text which location gives it its status, for example
  "Formalized: Mathlib states this exactly (proved).", naming the store's `basis`, not
  simply the rank-1 location. A survived coverage claim (decision D1) adds a line with
  its parts listed in the claim's order (library, declaration, proof status): "Formalized
  by 2 parts together:" when every part is finished, or "Stated by 2 parts together, not
  all of them finished:" (check 6). The builder gives every survived claim one of the two;
  for a store built before parity round three, whose claim may carry no status, the line
  reads "Covered by 2 parts together, not all of which count toward the status:". A part the
  claim confirmed says "a challenger checked it" on its card. When the claim is the basis that line is the status sentence;
  otherwise it follows the sentence. A claim that did not survive shows no line, and its
  parts are ordinary "states part" cards. Then the quote with its context, and a link to the passage
  in the pinned Wikipedia revision (a text fragment). The producer's item id
  (`In-text #<hex>`) is a `data-item` attribute, not reader-facing text. Passage
  permalinks (`#p-<hex>`) clear the sticky header (`scroll-margin-top`).
- **Location card.** One card per location: library chip, `@<sha7>`, the relation in
  words (exact match, close match, more general, special case, states part, related), and
  a proof-status badge. `statement only`, `wanted` and `axiom-dependent` are drawn as
  amber dashed alerts (the statement-only colour, not the not-formalized red) and are
  never hidden. Then the declaration (namespace faint, leaf bold,
  wrapping at `.` and `_`), its module and lines ("lines 612–640", or "line 88" when the
  record has no `end_line`), a convention note for close matches, the provenance line
  (rank, confidence, challenge), and "Source @<sha7>" linking
  `<repo>/blob/<commit>/<path>#L<line>-L<end_line>`, or `#L<line>` without an `end_line`.
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
  paths already start `src/`).
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
  coverage bar uses `style="width:…"`. Both are allowed by today's Report-Only CSP
  (`'unsafe-inline'`, with no nonce or hash that would void it), and a strict CSP would
  need hashes for the scripts and `style-src-attr` for the bar. Everything the pages, the
  article box and `atlas.js` load or fetch is same-origin, so enforcing the Report-Only
  policy as it stands breaks nothing (`wiki/e2e/atlas.spec.ts` runs the flows with it
  enforced). `atlas.css` and `atlas.js` are static assets, so they get the security headers
  from the generated `_headers` file, like every other asset.

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
It answers 503 when unbound, 403 for a cross-origin request (`src/security.ts`
`checkOrigin`), 401 without a session, and 403 for the pipeline bearer (these rows are
human labels). It is rate-limited per user on `EDIT_LIMITER` (key `atlas:<user>`, 429).
After those checks it reads the body through the shared `readJsonCapped`
(`src/security.ts`) with its own entry `MAX_JSON_BODY_BYTES.atlasFeedback` (4 KiB): a
declared or streamed body past it gets 413 "request body too large (max 4096 bytes)"
before parsing. It answers 400 for a bad field and 404 unless the location is on the
current store article. A campaign other than the store's gets **409** "stale", so a
verdict always names what the person saw. `revision` and `campaign` are copied from the
store, not taken from the client. Until migration 0015 is applied, the route answers 503,
not 500.

**`GET /api/atlas/feedback/export`** is for the pipeline bearer only (admin sessions get
403). It returns every row as NDJSON, paged from D1, with users only as
`sha256("atlas-feedback\0" + user_id + salt)[:12]`, where the salt is the research
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
7. **The producer contract notes** (section 3) other than the agreed format (note 10) need
   formal-companion's agreement before the first real set.
8. **Navigation.** `/atlas` is linked from atlas pages and from the article box, but not yet
   from the other shells' navigation. Adding it there changes cached bytes
   (`page:home`, `page:articles` and the render cache keys). *Recommendation:* add it in the
   release that binds the store.

## 15. Tests

- `scripts/atlas/test_build_store.py` (in `scripts/ci-python.sh`) checks fixture
  reproducibility, schema validity, the golden store, determinism, the manifest, the view
  model (including each passage's basis and the per-library proof split), title shards,
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
  `end_line`, closeMatch notes, minimal absence ends, `universe.yaml` refused), the other
  refusal cases (oldid, title
  keys, templates, dot-segment repos, display names, renderer, unknown statuses,
  non-string affixes), hostile values in some 75 fields of the records, sources, universe
  and seal (refused, never a crash), and the CLI (exit codes 2 and 3, and `--replace`
  refusing foreign content).
- `wiki/test/atlas.test.ts` (in `npm run test:ci`) checks 503 when unbound, the APIs and
  pages on the fixture store, 404s, escaping and integer-only numbers, the headings, the
  visible status sentence and its basis, the shard cache and the search limiter, feedback
  auth, origin, validation, size cap (the shared `readJsonCapped` cap, exact to the byte),
  rate limit and missing table, the export and its domain-separated pseudonyms and their
  `RESEARCH_EXPORT_SALT` salt, the security headers on every atlas response and the pages'
  CSP fit (same-origin loads only, no inline handlers), the article box with its cache
  isolation and revision note, the readers, non-array universe lists, related-only and
  Prop-valued-definition passages, the subject passage's marking and its Subject row, a
  `challenge_pending` location ("not challenged yet"), the coverage claim's line ("Formalized
  by 2 parts together" with the parts listed, only for a survived claim; its wording for a
  statement-only claim and for one an older store gives no status; a malformed claim never
  reaching the HTML), the theorem a data declaration formalizes, a location without `end_line`
  ("line 88", `#L88`), a run-token campaign on the pages and
  in feedback (`CAMPAIGN_RE`, the same grammar as the builder's), the producer's truth
  table and the shared parity table's claim-free cases for the page's fallback rows, and
  the library registry (seeded names, look-alike
  display names, ignored templates, repository-relative paths, dot segments,
  GitHub-only repository links).
- `wiki/test/retired-routes.test.ts` now excludes `/atlas` from the deleted paths, but still
  checks that `atlas` is reserved.
- `wiki/e2e/atlas.spec.ts` (the required browser job since PR #72) runs axe in both colour
  schemes at desktop width and at 375px (the Gadget page with its coverage claim's parts
  list included), plus the parts list itself, the combined filters, feedback, live search,
  a passage permalink, the Subject row's link and the article box, and runs those flows
  again with the Worker's
  Report-Only CSP enforced, asserting no violation.
- `wiki/test/request-limits.test.ts` puts the feedback route in the shared table of JSON
  body caps and scans `wiki/src` so that no route reads a JSON body except through
  `readJsonCapped` with its own `MAX_JSON_BODY_BYTES` entry.
