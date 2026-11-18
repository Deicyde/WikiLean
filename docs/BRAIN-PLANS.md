# Brain plans: the Tau Ceti plan producer (`wikilean.brain.plans/v1`)

Contract, 2026-11-18. Code: `scripts/tauceti_plans/`. Release-neutral: nothing in the Worker,
the public assets or a Brain release reads it yet. Context: [`BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md)
and [`BRAIN-D1.md`](BRAIN-D1.md) (one producer per source, each a sealed, pinned export that
becomes one D1 serving-dataset slice).

The Brain maps what Lean libraries contain. This producer adds what Tau Ceti *plans*: the
roadmaps of [TauCetiProject/TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap),
their layers and coverage states, the dependencies between them, the Lean names they mention
and the literature they cite.

## Commands

Acquisition and replay are separate, as elsewhere in WikiLean.

```bash
python3 -m scripts.tauceti_plans fetch --inputs DIR          # network; writes DIR/PINS.json
python3 -m scripts.tauceti_plans build --inputs DIR --out OUT # offline, deterministic
python3 -m unittest discover -s scripts/tauceti_plans -p 'test_*.py'
```

Python 3.12, standard library only (`tomllib` for `metadata.toml`). `build` runs `git` locally
to read the two checkouts and nothing else.

## Inputs

| Input | Where under `--inputs` | Pin |
|---|---|---|
| TauCetiRoadmap | `TauCetiRoadmap/` (git checkout) | HEAD commit; files are read from the commit with `git cat-file`, so a dirty working tree cannot leak in |
| trough | `trough/` (sparse checkout, `data/tauceti/`) | HEAD commit, plus `source.json`'s `rev` (the Tau Ceti commit trough read) and each file's sha256 |
| Tau Ceti atlas | `atlas-data.json` (the JSON embedded in https://taucetiproject.org/atlas/, built by CBirkbeck/tauceti-explorer) | sha256, `taucetiProgress.exportedAt`, the edition and the roadmap snapshot head it was built from |
| Mathlib declarations | `decls/mathlib.bmp` (doc-gen4 `declaration-data.bmp`, JSON) | sha256 and URL |
| Tau Ceti declarations | `decls/tauceti.bmp` | sha256 and URL |

Pins come from `PINS.json` (written by `fetch`) or, failing that, `run.sh`'s `PINS.txt`. `build`
recomputes every sha256 and commit and refuses (exit 1) when one differs from its pin. The docs
commit behind each `.bmp` is not in its bytes, so `docs_commit` is `null`.

## Output

Six JSONL files and `manifest.json`. JSONL is canonical: keys sorted, `ensure_ascii=False`,
separators `(",",":")`, LF, one row per line, rows sorted by `id`, ids unique. Two builds of
the same inputs are byte-identical.

A **pointer** is `{repo, commit, path, line}` into the pinned TauCetiRoadmap commit (`line`
1-based; omitted when the pointer is a whole file).

### `roadmaps.jsonl`

A roadmap is a folder under `TauCetiRoadmap/` or `Completed/`, at any depth, that holds a
`README.md`. Nested folders (`TauCetiRoadmap/RepresentationTheory/AdoIwasawa`) are roadmaps too.

| Field | |
|---|---|
| `id` | `tauceti:<folder path>`, e.g. `tauceti:TauCetiRoadmap/ModularForms` |
| `title` | the README's first `#` heading, a leading `Roadmap:` removed |
| `origin` | `active` (under `TauCetiRoadmap/`) or `completed` (under `Completed/`) |
| `topic` | `topic` from the folder's own `metadata.toml`, else `null` (not inherited) |
| `parent` | the enclosing roadmap's id for a nested roadmap, else `null` |
| `source` | pointer to the README (no line) |

### `layers.jsonl`

A layer is a README heading (level 2 to 6, outside code fences) whose rendered text starts with
a **key**: `Layer|Milestone|Stage|Part|Lane|Phase|Level|Step <token>` (`Layer 5`, `Layer 2.1`,
`Layer AI-0`, `Part A`) or a bare code `[A-Z]{1,4}<digits>(.<digits>)*[a-z]?` (`Y1`, `R2.3`).
The key must end the heading or be followed by `:`, a dash, `. `, `(`, `)` or `,`; so
`Layer 3 prerequisites` and `F0.1 constructions` are not layers. The vocabulary is the one the
coverage comments use.

| Field | |
|---|---|
| `id` | `<roadmap id>#<anchor>` |
| `roadmap`, `key`, `title` | the title is the heading's rendered text |
| `state` | `done`, `partial`, `untouched` or `unassessed` |
| `status_source` | pointer to the `STATUS.md` line holding the coverage comment the state came from, or `null` |
| `status_readme_current` | whether the comment's `readme_sha` is the sha256 of the README at the pin (`null` without a state) |
| `status_tauceti_rev` | the comment's `to_sha`, the Tau Ceti commit it assessed |
| `atlas_id` | the atlas's id for the same heading when it differs from ours, else `null` |
| `source` | pointer to the heading line |

**Anchor rule (GitHub's, as github-slugger).** Take the heading's rendered text (link labels
kept, `*` and backticks dropped, code-span contents kept), lowercase it, drop every character
that is not a Unicode letter, mark, number, connector punctuation (`_`), space or hyphen, and
turn each space into a hyphen. A slug already used in the same README (counting every heading,
not only layers) gets `-1`, `-2`, ... in document order. The atlas uses its own slugger, which
turns some punctuation into hyphens (`layer-3-abhyankar-s-lemma` where GitHub gives
`layer-3-abhyankars-lemma`); where the two agree the ids are the atlas's, and where they differ
the GitHub anchor is the id (it resolves on github.com) and the atlas's is kept in `atlas_id`.
An atlas stage is matched to a layer by equal id, else by equal title, else by equal key, README
path and line.

**States.** Every `<!--tauceti-coverage:v1 {...}-->` line in every `STATUS.md` is read; its
`roadmap` field is resolved under the same top folder as the `STATUS.md` (a parent's `STATUS.md`
may carry its sub-roadmaps' comments). A layer takes the `state` of the entry whose `id` equals
its key exactly. Everything else is `unassessed`: no comment, no entry, or an entry the rules
below reject. Rejected (in `rejected.jsonl`, layers stay `unassessed`): an entry with no layer
of that key (`coverage_no_matching_layer`), a key two layers of the roadmap share
(`coverage_ambiguous_layer_key`), a repeated entry id, an unknown state, two comments for one
roadmap, a comment for a folder that is not a roadmap, an unparseable comment. The comment's
`remaining` text is never copied.

### `edges.jsonl`

| Field | |
|---|---|
| `id` | `depends:<source>-><target>` |
| `kind` | `depends`; direction is the atlas's, prerequisite → consumer |
| `source`, `target` | roadmap or layer ids (ours, after mapping atlas stage ids) |
| `evidence` | the first verified pointer by (path, line); `more_evidence` holds the rest |
| `via` | `atlas`; `atlas_kinds` lists the atlas edge kinds merged into the row |

Candidates are the atlas's `edges` and `stageEdges`. An evidence item is verified only if, at the
pinned commit: its file exists and is the README of one endpoint's roadmap; its line exists;
the line holds the atlas's `match` text when the atlas gives one (the atlas's `sourceLine` then
points at the heading, so the lines of its quoted block are searched for the match); and the line
**names the other endpoint's roadmap**: its folder's last component appears with no letter or
digit either side (a relative link to the folder satisfies this; `GammaRay` does not name
`Gamma`). The atlas's evidence paths are mapped: `content/tau-ceti/X` → `TauCetiRoadmap/X` (or
`Completed/...`), `https://github.com/TauCetiProject/TauCetiRoadmap/blob/<ref>/<path>` →
`<path>`. The line number is the atlas's, not searched for: a line that moved since the atlas
snapshot fails. An edge is kept when one of its items verifies. A dropped edge goes to
`rejected.jsonl` (`kind: edge`); a failed item of a kept edge goes there as `kind: edge_evidence`.
Stage edges whose evidence is only "README order" or reviewer quotes have no line and are
rejected (`no_evidence_line`). The same rule applies within one roadmap, so an intra-roadmap
stage edge is kept only if its line names the roadmap's own folder.

### `decl_refs.jsonl`

One row per (layer or roadmap, Lean name).

| Field | |
|---|---|
| `id` | `<layer id or roadmap id>::<name>` |
| `name`, `roadmap`, `layer` (`null` for a roadmap-level name) | |
| `resolution` | `mathlib`, `tauceti`, `stated` or `planned` |
| `resolved_by` | list of `{input, sha256}` the resolution used; `null` for `planned` |
| `evidence` | pointer to the first occurrence; `occurrences` counts them |

Names come from (1) inline code spans in a README, outside fenced blocks, whose whole content
is one dotted Lean name (`Foo.bar`, segments not starting with a digit; a leading `_root_.` is
dropped); and (2) declarations in the roadmap's `Suggested.lean` (`theorem`, `lemma`, `def`,
`abbrev`, `structure`, `class`, `inductive`, named `instance`, `axiom`, `opaque`, after any
attributes and modifiers), with `namespace X` ... `end` prefixed, `section` scopes adding
nothing, `_root_.` making a name absolute, and comments skipped.

Attachment: a README line belongs to the innermost layer whose section holds it (a section runs
from its heading to the next heading of the same or a higher level); lines outside every layer
belong to the roadmap. In `Suggested.lean` a name belongs to the nearest preceding marker, a
markdown heading inside a `/-! ... -/` module doc; a marker whose key is a unique README layer
key gives that layer, any other marker gives the roadmap (a marker key with no README layer is
also logged as rejected).

Resolution is exact string equality against the `declarations` keys, in this order: Mathlib's
`.bmp` → `mathlib`; Tau Ceti's `.bmp` → `stated` if trough's `deps.json` entry for the name has
`sorry: true`, else `tauceti`; otherwise `planned`. No similarity, case-folding, prefix or suffix
matching: `Nat.Succ_le` and `Nat.succ_le_iff` stay `planned` next to a real `Nat.succ_le`.
Rejected rather than `planned`: a name ending in a file suffix (`Foo.lean`), an exact module
name in either `.bmp`'s `modules`, an unmatched `Mathlib.*` name, and the roadmap repo's own
modules (`TauCetiRoadmap.<Folder>[.Suggested]`).

### `xrefs.jsonl`

| Field | |
|---|---|
| `id` | `<layer id or roadmap id>::<xref>` |
| `xref` | `xref:stacks:<TAG>` or `lit:<yymm.nnnnn>` |
| `system` | `stacks` or `arxiv` |
| `roadmap`, `layer`, `evidence`, `occurrences` | as in `decl_refs` |

Scanned: every line of the README (fenced blocks included) and of `Suggested.lean`, attached as
for `decl_refs`. **Stacks**: a four-character token of `0-9A-Z` with at least one digit, not part
of a longer word or of a decimal, after the word `Stacks`, `Tag` or `Tags` (case-sensitive) or a
`stacks.math.columbia.edu/tag/` URL on the same line. An all-digit token from 1900 to 2099 is
rejected as a year. **arXiv**: on a line that says `arXiv` (any case), `yymm.nnnn` (to 1412) or
`yymm.nnnnn` (from 1501), month 01-12, from 0704 on, version dropped; any other shape is
rejected as `invalid_arxiv_id`, and old-style ids (`math/0601001`) as `old_style_arxiv_id`.

### `rejected.jsonl`

Every dropped candidate: `kind` (`coverage`, `edge`, `edge_evidence`, `decl_ref`, `xref`,
`lean_marker`, `roadmap`), `reason`, what was dropped (`candidate`, `source`/`target`,
`layer_key`) and a pointer where there is one. `id` is `<kind>:<first 20 hex of the sha256 of the
canonical row>`; identical rejections are one row with `count`.

### `manifest.json`

`schema`, `producer` (name, version), `inputs` (each pin: commit or sha256, URL, `fetched_at`,
and the extra facts above), `files` (rows, bytes, sha256 of each JSONL), `counts` (layers by
state, decl refs by resolution, xrefs by system, rejections by `kind/reason`), `clock_fields`,
`attribution`, and `logical_root`: the sha256 of the canonical JSON of the manifest without
`logical_root` and with every `fetched_at` removed. A refetch of identical bytes therefore keeps
the root. The atlas's `exported_at` is part of its pinned bytes, not a clock of ours, and stays in.

## Licence

TauCetiRoadmap and Tau Ceti are Apache-2.0 (TauCeti contributors); tauceti-explorer is
Apache-2.0; Mathlib is Apache-2.0. The manifest's `attribution` block says so. The export
stores pointers, not prose: roadmap and layer titles and keys, coverage states, Lean names,
Stacks tags and arXiv ids are copied; README sentences, `STATUS.md` prose, the coverage comment's
`remaining` text, the atlas's evidence text and docstrings are not. A test checks this on the
fixtures.

## Not yet

- No importer: the export is not loaded into D1 or the Brain. The slice's table layout is for
  the D1 work ([`BRAIN-D1.md`](BRAIN-D1.md)) to fix.
- No scheduling: nothing runs `fetch` or `build` on a cadence.
