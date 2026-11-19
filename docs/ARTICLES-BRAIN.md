# Articles → Brain: the proposal producer (`wikilean.brain.articles/v1`)

Contract, 2026-11-19. Code: `scripts/articles_brain/`. Release-neutral: nothing in the
Worker, the public assets or a Brain release reads it. Policy:
[`ARTICLES.md`](ARTICLES.md) section 13, "The Brain path". Conventions:
[`BRAIN-PLANS.md`](BRAIN-PLANS.md), the first producer of this shape.

The formalization reader records which Lean declaration states a passage of a Wikipedia
maths article. The Brain learns none of it today. This producer turns those findings
into Brain **proposal rows**, so one judgment is used twice and the two cannot drift:
nobody writes the same fact into both.

It proposes. It writes nothing: not the Brain, not the store, not D1.

## Commands

```bash
python3 -m scripts.articles_brain fetch --inputs DIR                 # network; pins DIR/PINS.json
python3 -m scripts.articles_brain fetch --inputs DIR --pins PINS.json # exactly these bytes
python3 -m scripts.articles_brain build --inputs DIR --out OUT       # offline, deterministic
python3 scripts/articles_brain/test_fetch.py
python3 scripts/articles_brain/test_build.py
```

Python 3.12, standard library only. `build` is offline: no network, no clock, no host
data. It writes the export to `OUT/<logical_root>/` and refuses an `OUT` inside a git
work tree — an export is evidence, never a commit.

## Acquisition

`fetch` reads five public, read-only GET routes and nothing else:

| Route | For |
|---|---|
| `/api/articles/current` | the published build: campaign, universe, record-set seals, index shards |
| `/api/articles/index/<shard>` | the build's page ids — the only way to enumerate the articles |
| `/api/articles/universe/<sha>` | the libraries, each with its `brain_lib` token |
| `/api/articles/article/<pageid>` | the sealed view model |
| `/api/articles/overlay/<pageid>` | that article's community overlay |

It sends `User-Agent: WikiLean-articles-brain/1` and no other identifying header, makes
one request at a time with a delay between them (`--delay`, 1 s by default), and backs
off on 429, 500, 502, 503 and 504 honouring a numeric `Retry-After` (five attempts, at
most 60 s each). Any other status fails the run.

**The store build is the pin.** `PINS.json` records the sha256 of every answer's bytes,
with the overlay's row count and highest `version` per article. `--pins` replays exactly
those bytes: on any mismatch it exits 1 naming the input, never overwrites an input with
the mismatched bytes, and leaves no `PINS.json`.

**It refuses to mix two store builds.** Every article must name the build's universe
sha256 and one of its record-set seals, every overlay must be its own article's, the
index must hold as many pages as `current.json` says, and `current.json` is read again
at the end and must be byte for byte the first answer — a republish mid-run refuses the
whole run.

**And it refuses a part of one.** A pins file is data, not trusted configuration: it can
be replayed and edited, so `build` makes every one of those checks again over the pinned
bytes, and also requires that the pinned shards are the build's index shards, that the
pinned pages are exactly the pages that index holds, and that no pin names a file
outside `--inputs`. An export carries the whole build's identity, so it is built from
the whole build or not at all.

## What it emits

Two files and a manifest, under `OUT/<logical_root>/`. JSONL is canonical: keys sorted,
`ensure_ascii=False`, separators `(",",":")`, LF, one row per line, rows sorted by their
key, keys unique. Two builds of the same inputs are byte-identical, and a refetch of
identical bytes keeps the `logical_root`.

### `proposals.jsonl`

Brain proposal rows **in exactly the shapes `brain/fold_proposals.py` accepts today**,
ready to be copied into `brain/proposals/` as one shard. Two shapes:

| Shape | Row | What the fold mints |
|---|---|---|
| `discover` (no `action`, a `decl`) | `qid`, `qid_label`, `decl`, `module`, `match_kind`, `confidence`, `proposer`, `evidence` | `{"src": qid, "dst": "decl:Mathlib:<decl>", "kind": "formalizes"}` in `brain/data/discovery_proposals.jsonl` |
| `repo_link` (`action: "repo_link"`) | the same, plus `repo` (a `frontier_sources` key) and `kind: "mentions"` | a row in `catalog/data/<repo>_links.jsonl` |

**One added field, namespaced: `wikilean_articles`.** It carries the evidence pointers
(below). The fold copies only the fields it knows into `brain/data`, so an unknown key
rides along and is ignored; a test asserts `brain/fold_proposals.py` never names it. The
human-readable `evidence` string carries the same pointers, so nothing is lost when the
fold drops the block.

### `rejected.jsonl`

Every candidate that did not qualify, with its `reason` and the same pointers. `id` is
`<kind>:<first 20 hex of the sha256 of the canonical row>`; identical rejections are one
row with `count`. A candidate is one merged location of one passage, or one community tag
whose passage is not in the article.

### `manifest.json`

`schema`, `producer`, `policy`, `fold`, `site`, `store` (the build's identity), `inputs`
(every pin), `options`, `channels`, `files` (rows, bytes, sha256), `counts`,
`clock_fields`, `attribution`, and `logical_root`: the sha256 of the canonical JSON of
the manifest without `logical_root` and with every `fetched_at` removed.

## The rules, and how each follows section 13

Section 13 says: the articles plane never writes the Brain; findings reach it only as
`brain/proposals/*.jsonl` through the offline fold; **concept edges come only from
`subject` passages whose own QID was validated against a sealed Wikidata bundle**; each
library maps to one `brain_lib` token; and **non-Mathlib libraries stay mentions-only**.

1. **Proposals only, through the offline fold.** The producer's whole output is proposal
   rows. It has no write path to anything.
2. **`subject` passages only.** A candidate whose passage is not `prominence: "subject"`
   is rejected (`not_subject_passage`). On the real store that is 5,521 of 5,587
   candidates: the gate does nearly all the work.
3. **Its own QID.** The row's `qid` is the article's `wikidata`, which under
   `cf-locate-recordset/2` is exactly what a subject passage's record carries as
   `identifiers.wikidata` — the builder refuses a subject end whose identifiers are
   anything else, and refuses them on a non-subject passage (decision B5,
   [`ARTICLES.md`](ARTICLES.md) section 3). An article with none, or with a
   non-canonical one, is rejected (`article_has_no_qid`, `qid_not_canonical`; the regex
   is the fold's own `QID_RE`).
4. **Validated against a sealed Wikidata bundle** — *by the fold, which is where the
   bundle is*. `fold_proposals` plans the exact QID set, refuses to run without the
   sealed bundle that covers it, and rejects a row whose QID is missing upstream or
   whose `qid_label` disagrees with the entity's label and aliases. The producer is
   offline and cannot do this; it supplies the label claim the gate needs (below) and
   leaves the validation where the evidence is.
5. **One `brain_lib` token per library.** The token comes from the universe, and a
   channel is keyed by the token **and** the repository the universe gives it, so a
   universe cannot move a library onto another library's channel by relabelling it
   (`wiki/src/libraries.ts` guards the same thing for display names). A location whose
   own `repo` disagrees with its library's is rejected (`library_repo_mismatch`).
6. **Non-Mathlib is mentions-only.** Mathlib is the one `formalizes` channel. Every
   other library goes through the fold's `repo_link` channel, which folds `mentions` and
   nothing else — "AI joins never mint identity claims", its moderation contract. A
   library with no `frontier_sources` harvest for the fold to check the declaration
   against is rejected (`no_brain_channel_for_library`); today that is Lean core,
   Batteries, Atlas and the FormalFrontier libraries.

### `formalizes`: concept QID → Mathlib declaration

All of: the passage is `subject`; the article carries a canonical QID and a title; the
location stands after the overlay merge; its relation is not `relatedMatch`; its
declaration is a Lean name; its library's token is `Mathlib` at Mathlib's repository; the
location came from the **record set**, not the overlay; **the record set's own relation
is the one the row rests on**; the passage's `outcome` is `located` and its `status` is
`formalized`; and **the store's own rule says this one location gives the passage
`formalized`**.

That last check is not a re-derivation: it *imports*
`scripts.articles.build_store.location_status` and calls it with the passage's kind and
the article's own `seal.status_rule`, so there is nothing here that can drift from the
plane. A part of a coverage bundle gives `partial` and stops there — no single
declaration states a passage a bundle covers — and so does any location the store counts
as weaker (`location_does_not_formalize`).

`formalizes` is many-to-many by construction (SCHEMA.md), so a subject passage with two
Mathlib declarations that each state it gives two rows, each with its own `match_kind`.

`match_kind` carries the relation: `exact` for `exactMatch`, and otherwise the store's
own token, `closeMatch` or `broadMatch`. SCHEMA.md rule 1 **fuses** concept and decl both
ways on `match_kind == "exact"`, so only the store's identity claim gets it; rule 2
attaches a concept to a cell on `generalization`/`special_case`, and how wide that rule
should be is explicitly Jack's call, so the producer uses a token that merges nothing.
It is not quite inert: a `formalizes` edge whose `match_kind` is in neither merge set
becomes a **synapse** carrying that token as its bond label (`build_cells.py`, rule 3),
so wiring this in would put `closeMatch`/`broadMatch` bonds in the graph. See "Left for
Jack".

### `mentions`: every other library

The passage is `subject`, the article carries a canonical QID, the location stands after
the merge, its relation is not `relatedMatch`, its declaration is a Lean name, and its
library's token maps to a `frontier_sources` key:

| `brain_lib` | repository | `repo` |
|---|---|---|
| `TauCeti` | `TauCetiProject/TauCeti` | `tauceti` |
| `FormalConjectures` | `google-deepmind/formal-conjectures` | `formal_conjectures` |

No status is required: a mention is the weak kind, and a declaration found relevant to a
statement about the concept is what it claims. A tag on a passage the store publishes as
`unchecked` ([`ARTICLES.md`](ARTICLES.md) section 9a) is a mention like any other — the
overlay may give such a statement a declaration, and that is a finding even though
nothing has searched for it.

### Community tags count only when accepted

The producer ports the location half of `wiki/src/articles-overlay.ts` `mergeOverlay`, so
"accepted" is the site's own answer:

- a tag at a sealed location's `pair_key` **is** that location: `state: "removed"`
  withdraws it, and a live tag may change its relation;
- a tag at a pair the pipeline never located is its own location, with
  `proof_status: "statement_only"` — the site never mints a formalization claim on one
  editor's word;
- a removed row, on either side, produces nothing (`withdrawn_by_the_overlay`), and a
  community removal of the pipeline's own basis location withdraws the `formalizes` row
  too;
- a live row that **relabels** a sealed Mathlib location withdraws its `formalizes` row
  as well (`community_relation_change_on_a_mathlib_location`). The relation *is* the
  identity claim — rule 1 fuses on `exact` — so one editor's, or one agent's, upgrade of
  a `closeMatch`, a `broadMatch` or a `relatedMatch` must not mint or strengthen an edge,
  and their downgrade of an `exactMatch` is a dispute, not a weaker claim to publish.
  The write route takes any of the six relations at a sealed pair key
  (`wiki/src/articles.ts`), so this is the ordinary case, not an exotic one. A note-only
  change leaves the row standing, and the mentions channel is unaffected: there every
  row is `mentions` whatever its relation;
- a tag whose `passage_key` is in no passage of the article is rejected
  (`tag_passage_not_in_the_article`).

Because a community location is `statement_only`, a community tag *of its own* can never
make a passage `formalized`, so it can never reach the `formalizes` channel that way
(relabelling a sealed location is the other way, above). The producer makes that
explicit: a Mathlib community tag is rejected
(`mathlib_community_tag_has_no_mentions_channel`) rather than quietly promoted, because
the fold has no Mathlib mentions channel to put it in. See "Left for Jack".

### Withdrawal: absence is not enough, and the fold must change

The export is the **current state**: a withdrawn tag is simply absent from the next one.
The fold handles that correctly for one channel and not the other.

- **`discover` → `brain/data/discovery_proposals.jsonl` is regenerated whole** on every
  fold. A row that stops being proposed stops being in the output. Absence works.
- **`repo_link` → `catalog/data/<key>_links.jsonl` is MERGED** with what is already in
  the file, minus only the keys **rejected in that same fold** (`repo_retract`). A row
  that merely disappears from the proposals is kept forever. **Absence does not
  withdraw.**

This is not papered over: nothing in this producer pretends otherwise, and no mentions
row claims to be withdrawable. The later wiring (release-affecting, and Jack's) must do
one of two things:

1. **Retraction rows.** Give the producer the previous export and have it emit, for every
   mentions key present then and absent now, a row with the same `(repo, qid, decl)` and
   `verdict: "reject"`. The fold's any-reject veto already turns such a row into a
   retraction of that key. This needs no fold change, but it makes the producer stateful.
2. **A whole-slice channel.** Teach the fold that this shard owns its slice of
   `catalog/data/<key>_links.jsonl` and regenerate that slice wholesale, as the
   `discover` channel already is. This is the cleaner shape and the one to prefer; it is
   a change to `brain/fold_proposals.py`, so it is Jack's.

Until one of them is in, treat the mentions channel as append-only and do not promise a
contributor that removing a tag removes the Brain edge.

## Evidence, and credit

Every row cites its evidence by pointer, never by copied prose. The block is
`wikilean_articles`:

| Field | |
|---|---|
| `store` | the sha256 of `current.json`'s bytes: which published build |
| `pageid`, `passage_key`, `pair_key`, `location_key` | where in it |
| `prominence`, `passage_kind`, `sealed_status` | the passage, as the store published it |
| `relation`, `proof_status`, `library`, `brain_lib`, `repo`, `commit` | the location |
| `base_relation` | the sealed relation, when a live community row changed the location: a row never cites the record set for a relation the record set does not state |
| `source` | `record_set` or `overlay` |
| `record_set` | the article's record-set seal sha256 (a sealed location) |
| `overlay_version`, `overlay_max_version`, `overlay_state`, `overlay_via` | the row's own version and the article's highest (an overlay row) |
| `origin` | `pipeline` or `community` |
| `actor_type` | `human` or `ai` |
| `evidence_count`, `more_evidence` | when the same join was found on more than one passage of the article |

**No passage text, no note, no signature and no Wikipedia prose is copied**, and a test
checks it. The mapping is CC0 ([`ARTICLES.md`](ARTICLES.md) section 14, decision 4); the
article text, which is CC BY-SA, is not here at all.

**`actor_type`** is `human` when a live overlay row by a person is the standing claim
(its own, or its confirmation or correction of a sealed location) and `ai` otherwise — a
locate run is a model run, challenged and audited, and the store records both.
`confidence` follows: `high` for a person, `medium` for an AI. The fold caps a row with
no skeptic verdict at `medium` whatever it says, so `high` is a statement about the
evidence and not a way round the cap.

**Credit is Jack's decision, so no user identity is carried.** `--include-author-identity`
adds each overlay row's author id and is **off** by default; the manifest records which
way the build ran. Names are never carried either way.

## How the fold consumes it (not wired; release-affecting; later)

Copy `proposals.jsonl` into `brain/proposals/` as one shard — say
`articles_brain_<logical_root first 12>.jsonl` — and run the fold the usual way: write
the canonical QID request plan, acquire the sealed Wikidata entity bundle for it if the
plan is non-empty, then fold with that exact bundle. The fold then applies, to every row:

- the decl existence oracle and the Mathlib checkout grep (a `discover` row), or the
  repo's harvest `catalog/data/<key>.jsonl` (a `repo_link` row);
- sealed Wikidata existence and label agreement;
- the confidence cap and `skeptic: "pending"`, since this producer writes no verdict;
- the any-reject veto across shards.

`brain/fold_proposals.py`, `brain/`, `catalog/` and the nightly are all untouched here.
Wiring this in is release-affecting and is Jack's call, together with a skeptic pass: a
`discover` row folds on `pending`, so the first wiring would publish `formalizes` edges
at capped `medium` with no second reader.

## Left for Jack

1. **Credit.** Carrying `actor_type` and not identity is the safe default, not a
   decision. If contributors are to be credited in the Brain, `--include-author-identity`
   is the lever and the licence and privacy wording is the work.
2. **Non-Mathlib `formalizes`.** Current policy is mentions-only. Four of the seven
   mentions rows in run 1 are FormalConjectures locations of a subject passage, one of
   them an `exactMatch`; those are identity claims the policy declines to make.
3. **A Mathlib mentions channel.** A community tag of a Mathlib declaration has nowhere
   to go: `discover` is `formalizes`-only and `mathlib` is not a `frontier_sources` key.
   Either add one (a `mathlib_links.jsonl` with the same mentions-only contract) or
   accept that community Mathlib tags never reach the Brain.
4. **`match_kind` for a non-exact relation.** 22 of run 1's 25 `formalizes` rows rest on
   a `closeMatch` or a `broadMatch`. They carry the store's own token, which merges
   nothing — but rule 3 keeps the edge as a synapse labelled `closeMatch` or
   `broadMatch`, a bond kind outside the Brain's own vocabulary. Mapping `broadMatch` to
   `generalization` and `narrowMatch` to `special_case` would let SCHEMA.md rule 2 attach
   those concepts to a cell; rule 2's width is marked in SCHEMA.md as yours.
5. **`qid_label`.** The producer claims the article's **title**, which is the only label
   the allowed routes give. Wikidata's label for the same item often differs by a
   disambiguator ("Distribution (mathematical analysis)" against "distribution"), and the
   fold's label-agreement gate then rejects a correct row. Failing closed is the right
   direction, but the better gate for this producer is `enwiki_slug` agreement, which the
   sealed bundle already carries. That is a fold change.
6. **Withdrawal**, above: the mentions channel cannot be withdrawn by absence today.

## Run 1 (2026-11-19)

The record of one run, not a claim about the current code: it predates the sealed-relation
gate and the `base_relation` pointer above, both of which change the rejection reasons, the
bytes of any row a live community row touches, and therefore the `logical_root`. Re-run
before quoting these figures.

Store build `c3bba3d5…` (`current.json`), campaign `tierA-b1`, universe
`12e9eacf…`, record sets `23710337…` and `f1e67e43…`; 48 articles, 4,285 passages, 37 of
them `subject`, 165 overlay tags (all live, none on a subject passage).
`logical_root` `61405537d18c86987501984f8870e3770f27276b571d2317c9df423aa28667a3`.

| | |
|---|---|
| `proposals.jsonl` | 32 rows, 43,396 bytes — 25 `formalizes` (3 `exact`, 5 `closeMatch`, 17 `broadMatch`), 7 `mentions` (`formal_conjectures` 4, `tauceti` 3), over 24 distinct QIDs. One row is `actor_type: human` (a person confirmed the pipeline's exactMatch on page 8564); the other 31 are `ai` |
| `rejected.jsonl` | 5,555 rows, 5,063,291 bytes — `not_subject_passage` 5,521, `relation_related_match` 14, `no_brain_channel_for_library` 10 (LeanCore 5, Atlas 4, Batteries 1), `status_not_formalized` 6, `location_does_not_formalize` 4 |

Of the 165 overlay tags, 163 sit on passages that are not `subject` and 2 on subject
passages, both at a pair the pipeline had already located: one person's confirmation of
an `exactMatch` (the human row above) and one agent's `broadMatch` on a passage the store
leaves `partial`. No community-only tag reached a row in this run.

## Licence

The mapping is CC0-1.0 (WikiLean contributors); the declaration names are each library's,
Apache-2.0. The manifest's `attribution` block says so. No article text is in the export.

## Not yet

- No wiring: nothing copies the export into `brain/proposals/` and nothing folds it.
- No cadence: nothing runs `fetch` or `build` on a schedule, and the nightly does not
  know this exists.
