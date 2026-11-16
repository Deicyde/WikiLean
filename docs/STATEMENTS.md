# `wikilean.statement-set/1` — publishing a cut on its own

The maintainer's ruling of 2026-11-12 splits the **cut** (which sentences of an article
state something, and where their edges are) from **locate** (whether Lean has them), and
publishes the first alone. This is the format phase 1 publishes.

Where the design lives: `~/wl-phase1/DESIGN.md` sections 1 and 2. Where the cut itself
lives: [PIPELINE.md](PIPELINE.md) stage 9b (`scripts/pipeline/cut.py`). Where the keys
live: [PIPELINE.md](PIPELINE.md) stage 2 (`scripts/pipeline/keys.py`).

| what | where |
|---|---|
| the writer | `scripts/pipeline/export_statements.py` (stage 6b) |
| the reader | `scripts/pipeline/statements.py` (stage 4b) |
| the synthetic set | `scripts/pipeline/fixtures/statementset/`, written by `fixtures/make_statement_fixture.py` |
| the tests | `scripts/pipeline/test_statement_sets.py` |
| the vocabulary | `scripts/pipeline/vocab.py`: `STATEMENT_SET_SCHEMA`, `STATEMENT_SEAL_KEYS`, `STATEMENT_CUT_RULE_KEYS`, `STATEMENT_KEYS`, `STATEMENT_OPTIONAL` |

## 1. Why a new schema and not a record-set profile

The obvious shortcut is to export a cut as a `cf-locate-recordset/2` whose records are
all the *unresolved* shape (`object: []`, no status, no relation). It does not work and
should not:

1. **A record set must carry a `universe.json` with a non-empty library list**
   (`build_store.py`'s `load_universe`). A statement set searched nothing, so naming
   libraries would publish a false claim about what was looked at.
2. **Every record must carry a `challenge` value** (`records.row_challenge`). Nothing
   judged a cut, so there is no honest value to write.
3. **`unresolved` already means something else** — locate ran and settled nothing — which
   the reader renders as "No status yet", *uncounted and unhighlighted*
   (`wiki/src/articles-render.ts`). A cut exported that way would render invisibly.

So a statement set is a **sibling** of the record set, not a profile of it, built from the
same parts: `keys.py`'s key recipes verbatim, the same canonical JSON spelling, the same
seal grammar and token rules, the same 12-word quote cap and 32-character affix cap, and
the same cut rules the gate holds a document to.

**Nothing shared with formal-companion changes.** A statement set holds no Literature
Links record, so the vendored v1.0.2 pin is untouched; `cf-locate-recordset/{1,2}` and
`cf-locate/1` are untouched; `cf-sourceset/1` is read, not written. The `wikilean.`
prefix follows `wikilean.pipeline.carry/1` and says whose format it is.

## 2. The set

```
<set>/SEAL.json                      the pins, the cut rules, and every file's sha256
<set>/sources.jsonl                  one row per source
<set>/statements/<source-id>.jsonl   one statement per line
```

Every file is canonical JSON (`keys.canonical`: sorted keys, no spaces, UTF-8, no ASCII
escaping), one object a line, newline-terminated. **The set is a function of its inputs**:
the same cut, sources and pins write byte-identical files, which is what makes a
re-export a check rather than a new claim. Statements are written by span start, then
item; sources by source id; the seal's `files` map is sorted.

### `SEAL.json`

Eight keys, exactly:

| key | what |
|---|---|
| `schema` | `"wikilean.statement-set/1"` |
| `set_id` | a token (`vocab.TOKEN_RE`), the set's name |
| `campaign` | a campaign id, or `run:<run-id>[+<run-id>]` (`vocab.CAMPAIGN_RE`); never null |
| `sourceset` | `{id, manifest_sha256}` — the sourceset version the cut was planned against |
| `producer_commit` | the 40-hex commit of what produced the set |
| `created` | the newest harvest time, as ISO 8601 UTC, so a re-export is byte-identical |
| `cut_rules` | `{span_end_rule, statement_cut_rule}` — the rules the cut ran under |
| `files` | each relative path to the sha256 of its bytes |

Four things a record set's seal has and this one has **not**: `universe` (nothing was
searched), `status_rule` (nothing was derived), `byte_budget` (no receipts to budget) and
`skill_commit` — whose job `producer_commit` does. `producer_commit` is the name
`skill_commit` should have had (PIPELINE.md question Q2) and can be right here because
nothing shared reads it.

`cut_rules` is a record of the run, not a demand on the reader: a set sealed from an
older cut names that cut's rules. The writer's defaults are today's,
`span_end_rule: 3` and `statement_cut_rule: 4` (the sentence check of issue #172).

### `sources.jsonl`

One row a source, exactly `vocab.SOURCE_KEYS`: `source_id`, `pageid`, `revision`,
`title`, `wikidata`, `html_sha256`, `layer_sha256`, `renderer`, `assessment`. These are
the sourceset entry's own fields and nothing more. The permalink, the container title and
the SPDX licence are **not** written: they are facts about the source's *family*, which
`scripts/pipeline/sources.py` holds, and a consumer derives them from the `enwiki-` source
id rather than trusting a set to state them. A source whose family may not be quoted keeps
its statements out of the set, exactly as it keeps its records out of a record set.

One revision of one page per set: two rows with one `pageid` are refused.

### `statements/<source-id>.jsonl`

One statement a line. Thirteen keys always, and two more on a compound:

| key | what |
|---|---|
| `item` | `In-text #<8 to 16 hex>` — the frozen head rule's id (`gate/locate_text.assign_ids`) |
| `passage_key` | `sha256("<pageid>\n<span_sha256>")`, recomputed here, never copied |
| `span` | `[start, end]`, half-open code-point offsets on the layer whose sha is the source's `layer_sha256` |
| `span_sha256` | the sha256 of the span's characters as they stand, trailing whitespace included |
| `head_words` | how many words the head rule hashed |
| `quote` | `{exact, prefix, suffix}` — **derived**: the span's first 12 words, made unique by as little context as it takes (`records.cut_passage`), the affixes empty when it is already unique |
| `section` | the heading path, `[]` in the lead |
| `fragment` | the section's fragment, or `null` |
| `passage_kind` | one of `vocab.KINDS` |
| `prominence` | one of `vocab.PROMINENCES`; at most one `subject` an article |
| `scope` | `formal_math`, `expository` or `non_math`; only the first is ever located |
| `statement` | the worker's one-sentence restatement (non-empty on a `formal_math` passage) |
| `finder` | one of `vocab.FINDERS`: the three deterministic finders, or `model` |
| `conjuncts` | on a compound only: 2 to 5 `{id, text}`, ids distinct |
| `shape` | on a compound only: one of `vocab.COMPOUND_SHAPES` |

`conjuncts` and `shape` ride together, on a `formal_math` passage only. A conjunct of a
cut is `{id, text}` and **nothing else**: the `part` a coverage bundle's conjunct carries
is a location key, and no location exists yet.

That is exactly `run/plan.py`'s `LOCATE_PASSAGE_KEYS` — less `text_span`, which is a fact
about the local render and is recomputed, not sealed — plus the two keys and the cut's own
`fragment`, `scope` and `finder`. So phase 2 needs nothing else. The cut's remaining
bookkeeping (`concept`, `until`) is not sealed: the span is.

**No universe, no receipts, no `challenge`, no status.** The set states what was cut, and
nothing about Lean.

**Why the quote is derived and not copied.** A cut passage carries the quote the worker
found it at, which gate rule 2 holds to being the one text at the span and nothing more:
in a real run it is whatever words the worker wrote down. The quote a *record* carries is
the format's capped head — `records.cut_passage`, which the tally derives for every
record set. Phase 1 publishes that one, so the passage a reader tags in phase 1 and the
location the pipeline writes in phase 2 show the same words. The passage's own quote is
still checked (it must be the one text at the span, with its affixes) and then set
aside.

One consequence, and the only place a published cut and a harvested one differ
downstream: a locate task manifest planned from a statement set hands the worker the
capped head, where one planned from the harvested run hands it the worker's own quote.
Every other byte of every task manifest is the same — `test_model_stages.py` pins it —
so a run planned from a harvested cut, batch 1 included, is unchanged.

## 3. Why the keys cannot drift between the phases

`passage_key = sha256("<pageid>\n<span_sha256>")` and `span_sha256` is the sha256 of the
span's text on the pinned layer. Both are products of the cut alone — no universe, no
commit, no revision.

Phase 2 does not re-cut. `scripts/pipeline/statements.py`'s `frozen_inventory` reads the
sealed statements, re-renders the same pinned revision (the sourceset pins
`html_sha256`, `layer_sha256` and the `web-html/3` renderer), and **recomputes every
`span_sha256` and every `passage_key` from that layer, refusing the read on any
difference**. `frozen_inventory` also checks each sealed quote is still the one text at its span on
that layer. `manifest_problems` refuses a sourceset that has moved on from the revision
the set was cut from, before anything is planned.

So a community tag written on a statement in phase 1 is the same D1 row as the pipeline's
location in phase 2 ([ARTICLES.md](ARTICLES.md) 10a, "the key that makes it work").
`test_statement_sets.py` pins this twice: every sealed `passage_key` is
`keys.passage_key(pageid, keys.span_sha256(layer, span))`, and it is byte for byte the
`passage_key` `records.passage_records` writes on a record of the same passage.

## 4. What is refused

Statement by statement, naming each one in the export report and exiting 1 after writing
the rest:

- no `item`, or one outside the head rule's grammar (`vocab.ITEM_RE`);
- a span that is not `[start, end]` with `0 <= start < end`, or that leaves the layer;
- the cut's own quote missing, not unique on the layer with its affixes, not at the
  span's start, or running past the span's end (gate rule 2, kept here because the
  sealed quote is derived: a span that does not hold the words it was cut at would
  otherwise seal as a statement of the few characters the span does hold);
- a sealed quote that is empty, over the 12-word cap, or whose affix is over 32
  characters — **a reader rule**: the writer derives the quote with
  `records.cut_passage` and cannot produce one outside those caps;
- no `head_words`, or one that is not a positive integer;
- a `passage_kind`, `prominence`, `scope` or `finder` outside its vocabulary;
- a passage that is not `formal_math` and not `supporting`;
- a `formal_math` passage with no restatement, or a restatement over 1000 characters;
- a restatement or a conjunct that carries the source verbatim past the quote cap
  (`records.check_free_text`: the cap gone round the back);
- any text UTF-8 cannot carry — a lone surrogate, which reaches a set only as a `\udXXX`
  escape in a hand-made line: everything written from a statement is UTF-8 (a record's
  canonical JSON, the store's view model), and the rule names the field rather than
  leaving the first writer to raise;
- no section heading path, or a heading that is blank; an empty `fragment`;
- `conjuncts` without `shape` or the other way about; conjuncts on a passage that is not
  `formal_math`; a shape outside the vocabulary; fewer than 2 or more than 5 conjuncts, or
  a shape whose conjunct count is fixed and does not match; two conjuncts with one id; a
  conjunct carrying a `part`;
- a stated `passage_key` or `span_sha256` that is not the one the layer gives;
- a `fragment` or a conjunct id that is padded with whitespace;
- a passage that is not an object at all;
- two statements of one source with one item or one span text (no key could tell them
  apart), two that share a character, and a second `subject`.

Whole sources, before any of their statements are written:

- a source whose family may not be quoted, or whose `source_id`, `pageid` and `revision`
  disagree;
- a source the sourceset does not pin, or whose `html_sha256`, `layer_sha256` or
  `renderer` is not one;
- a source whose rendered layer is missing, or does not hash to the `layer_sha256` the
  cut was made against;
- one source twice, or two revisions of one page.

And the whole export, before anything is written, when not one statement survived: an
empty sealed set would be a claim that a cut found nothing, which is never what
happened.

And the inputs, before anything is written: a `set_id`, `campaign`, `created`,
`producer_commit` or `sourceset_manifest` outside its grammar, a cut rule that is not a
positive integer, and a target directory that is inside this checkout, reached through a
symlink, or not empty and not an earlier statement set.

**The writer and the reader hold a statement to one rule set.** It is
`statements.statement_problems`: the writer projects a harvested passage into the sealed
shape and runs it over the result, and the reader runs it over the sealed bytes, so
neither side can enforce a rule the other does not. Three of its rules need the
article's text and so run only when the caller holds the pinned layer — the writer always
does, and the reader does inside `frozen_inventory(set, <rendered sources>)`:

- the quote is the one text at its span, with its prefix and suffix;
- the restatement does not carry the source past the quote cap;
- nor does any conjunct's text.

`statements.py --verify-only` therefore checks the seal and the sealed bytes and says so;
`--sources` is required otherwise. `StatementSet.cuts()`, which hands stage 6 the `Cut` a
record is written with, refuses a set no render has checked.

Three rules are the **reader's alone**, because the writer derives the value they judge:
the quote's 12-word cap, its 32-character affix cap, and the quote sitting inside the
span. The writer holds the *passage's own* quote to gate rule 2 (unique, at the span's
start, inside the span) and then derives the sealed one, which cannot break any of the
three.

**What the writer does not check: that the `item` is the id the head rule gives.** The
head rule reads the *head text*, which needs the source's `formulas.json` as well as its
layer, and it keys the whole table at once. That check is the gate's, at stage 7 (rule 2),
and it ran on the harvested document before the set was sealed. The writer holds the item
to its grammar and nothing more.

And the reader alone, over bytes no writer of ours could have produced: a line that is
not JSON or not an object, a blank line, a statement with a stray or missing key,
statements out of span-then-item order, a `sources.jsonl` row that is not exactly
`vocab.SOURCE_KEYS`, a source row with no statement, and a file the seal names that is
neither `sources.jsonl` nor a listed source's statements. Every reason is reported at
once.

## 5. What phase 2 does with one

```bash
# 9c: plan a locate run from a published cut instead of a harvested one.
python3 scripts/pipeline/locate.py --run-id loc-1 --out <root> \
    --statement-set <set> --sourceset <dir> --universe <file>     # refused beside --inventory

# 6: export the records of that run, their cut taken from the set.
python3 scripts/pipeline/export_records.py --rows <rows> --sources <sources> ... \
    --statement-set <set> --rendered-sources <dir>/sources
```

`locate.py --statement-set` refuses the plan unless the sourceset still pins the
revision the set was cut from (`statements.manifest_problems`), and recomputes every
`span_sha256` and `passage_key` against that revision's render before a task is cut.
`run.json`'s `locate.statement_set` records which set it was, by id and seal sha256; the
task manifests say nothing about it.

`export_records.py --statement-set` takes each passage's `Cut` from the set — the quote
and the `span_sha256` are the published ones — and refuses a row whose own cut states
another. It needs `--rendered-sources`, and the two go together and only together,
because the published cut is checked **twice** before a record is written from it:
against the render (every layer hashes to the `layer_sha256` the set was cut from, and
every span and quote is the sealed one) and against the **sources the export is about to
seal** (`manifest_problems` again: a record set whose `sources.jsonl` named another
revision or another layer would publish spans that are offsets on nothing). The layers
also hold every free-text field the export writes to the quote cap — which is why a
render is never read without a cut to check it against.

## 5a. A re-cut, and the tags of the cut before it

`carry.py` maps an old cut onto a new one from spans and span text alone, with the fates
`unchanged`, `end_moved`, `split`, `merged`, `reshaped`, `removed`, `changed` and `new`.
Either side may now be a sealed statement set (`--from-set` / `--to-set`), because the
map is a fact about text and not about where a cut was published.

**A tag carries only on `unchanged`** — the key is byte-identical, so nothing has to
move. On every other fate the text changed and no mechanical re-pairing is honest. The
D1 row is not deleted; it simply stops appearing, because the overlay merge is driven by
the store's passages.

So the map is published: `carry.py --store-out <dir>` writes one `<pageid>.json` a page,
and `build_store.py --carry <dir>` publishes them at `articles/v1/carry/<pageid>.json`.
Each entry carries the old passage's `passage_key`, `span`, `scope`, its **old quote**
and its `fate`, and the new passages it went into. That is what lets the reader show the
tags of an earlier cut in a fold — "from an earlier cut of this article", with the fate
and the old quote — and an editor re-apply one in a click. Nothing auto-moves.

The old quote is **derived** from the old layer (`records.cut_passage`), like every other
quote the reader shows, and not copied off the harvested document, whose own quote is the
words the worker was cut at and which nothing ever displayed.

`--store-out` holds its directory to being this map and nothing else: a page that drops
out of a later run is removed rather than left to go on being published, and a directory
holding anything but `<pageid>.json` documents is refused. A map with a **refused page**
in it is not published at all and the command exits 2: in the store a page nobody could
map and a page nothing moved on look the same, and only one of them is true. For the
same reason `--store-out` is refused beside `--source`: that run maps some of the
corpus, and publishing it would delete the document of every page it did not name. Map
the corpus in one run, or write `--out` documents and assemble the directory.

A sealed set used as a side is checked against the render too: the map's keys are
recomputed from the layer, so a set whose sealed `span_sha256` is not that layer's is
refused rather than mapped under keys the store never published.

## 6. Commands

```bash
# Seal a harvested cut run that lives beside its sourceset.
python3 scripts/pipeline/export_statements.py \
    --inventory <corpus>/inv-1 --sourceset <dir> [--sourceset-id <id>] \
    --out <set> --set-id <token> --campaign run:inv-1 \
    --producer-commit <40 hex> --created <ISO-Z> \
    [--span-end-rule 3] [--statement-cut-rule 4] [--source <id> ...] [--report <file>]

# Read one back: the seal, the rules, and every span against the pinned render.
python3 scripts/pipeline/statements.py --statement-set <set> --sources <dir>/sources
python3 scripts/pipeline/statements.py --statement-set <set> --verify-only

# Rebuild the synthetic fixture (the only set that may be written inside the checkout).
python3 scripts/pipeline/fixtures/make_statement_fixture.py
```

Exit codes follow `export_records.py`: 0 everything exported, 1 some statements were
refused and the set holds the rest, 2 the inputs were refused before anything was
written, 3 the writer failed.

**A statement set is never written inside this checkout.** WikiLean is public and a
statement quotes its article; only `fixtures/make_statement_fixture.py` may write one
here, and only under `scripts/pipeline/fixtures/`, which the command line cannot reach.

## 7. What a statement set does not decide

- **Boundaries stay the pipeline's.** The community edits tags, not spans: a
  community-invented span would mint a `passage_key` no sealed set will ever reproduce
  (DESIGN.md section 4). The complaint route — a passage-level "this is cut wrong" flag —
  is P7 and is not built here.
- **The tagging universe is not in the set.** A store built from a statement set is given
  one with `build_store.py --tagging-universe`, and its `(library id, repo)` pairs must be
  the ones phase 2 will search ([ARTICLES.md](ARTICLES.md) section 4a).
- **A re-cut carries nothing by itself** (section 5a).
- **Stage 9d, the challenge, is not wired to a published cut from the command line.**
  `statements.passage_index` gives `challenge.plan` the `{(source, item): passage}` it
  takes from `cut.passage_index`, so the piece is there; `challenge.py`'s own flags still
  name a harvested run.
