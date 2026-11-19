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
python3 -m scripts.tauceti_plans fetch --inputs DIR          # network; newest state; writes DIR/PINS.json
python3 -m scripts.tauceti_plans fetch --inputs DIR --pins PINS.json  # exactly these commits and bytes
python3 -m scripts.tauceti_plans build --inputs DIR --out OUT # offline, deterministic
python3 -m unittest discover -s scripts/tauceti_plans -p 'test_*.py'
```

Python 3.12, standard library only (`tomllib` for `metadata.toml`). `build` runs `git` locally
to read the two checkouts and nothing else.

**The export is never committed.** `decl_refs.jsonl` alone is about 43 MB and grows with the
roadmaps; GitHub refuses files over 100 MB, and large tracked files were the whole problem in
[`BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md). `build` refuses an `--out` inside a git work tree (any
ancestor holding `.git`) before it reads anything.

`fetch` sends every network call (HTTP GETs and git clones) through one `Opener`, which the
tests replace with an offline fake. With `--pins` (a `PINS.json`, or run 1's `PINS.txt`) it
checks out each git input at its pinned commit and checks every URL input's sha256 (and each
`docs_commit` the pins give) against the pin; on any mismatch it exits 1 naming the input, never
overwrites an input file with the mismatched bytes, and leaves no `PINS.json`.

**Release neutrality.** Every path here is release-neutral. Check with the classifier, run by
path: `site.ops.brain_release_authority` cannot be imported, because the standard library's
`site` module shadows the repo's `site/` package, so run
`python3 -I -B site/ops/brain_code_only_release.py classify <paths>`.

## Inputs

| Input | Where under `--inputs` | Pin |
|---|---|---|
| TauCetiRoadmap | `TauCetiRoadmap/` (git checkout) | HEAD commit; files are read from the commit with `git cat-file`, so a dirty working tree cannot leak in |
| trough | `trough/` (sparse checkout, `data/tauceti/`) | HEAD commit, plus `source.json`'s `rev` (the Tau Ceti commit trough read) and each file's sha256 |
| Tau Ceti atlas | `atlas-data.json` (the JSON embedded in https://taucetiproject.org/atlas/, built by CBirkbeck/tauceti-explorer) | sha256, `taucetiProgress.exportedAt`, the edition and the roadmap snapshot head it was built from |
| Mathlib declarations | `decls/mathlib.bmp` (doc-gen4 `declaration-data.bmp`, JSON) | sha256, URL and `docs_commit` |
| Tau Ceti declarations | `decls/tauceti.bmp` | sha256, URL and `docs_commit` |

Pins come from `PINS.json` (written by `fetch`) or, failing that, `run.sh`'s `PINS.txt`. `build`
recomputes every sha256 and commit and refuses (exit 1) when one differs from its pin. The docs
commit behind a `.bmp` is not in its bytes: `fetch` takes the first entry's `docLink`, fetches
that one module page relative to the docs root (the parent of `declarations/`), and reads the
40-hex commit from its `github.com/<owner>/<repo>/blob/<sha>/` source links
(`leanprover-community/mathlib4` for Mathlib, `TauCetiProject/TauCeti` for Tau Ceti); it is
`null` unless the page names exactly one, and always `null` with `PINS.txt`.

The atlas computed its line numbers on its own README copies, at
`meta.repositorySnapshot.head` (`faa5423b…`), a `local_working_tree` commit that no public
remote holds (not TauCetiRoadmap, not CBirkbeck's fork), so it cannot be pinned. The manifest
records it as `inputs.atlas.atlas_snapshot_commit` with `publicly_available: false`; the
README copies the atlas embeds (`roadmaps[].readme`) are pinned through `atlas-data.json`'s
sha256 instead.

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
`Layer 3 prerequisites` and `F0.1 constructions` are not layers, and a dash followed by another
key makes a range (`F0–F1`, `R7.1–R7.8`), not a layer. The vocabulary is the one the
coverage comments use.

| Field | |
|---|---|
| `id` | `<roadmap id>#<anchor>` |
| `roadmap`, `key`, `title` | the title is the heading's rendered text |
| `ordinal` | 0-based position among the roadmap's layer headings, in README order. Order is sequence, not a dependency claim, so it makes no edge |
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
| `kind` | `depends`, in `brain/SCHEMA.md`'s direction |
| `source`, `target` | roadmap or layer ids (ours, after mapping atlas stage ids): `source` is the consumer, `target` the prerequisite |
| `evidence` | the first verified pointer, public pointers first, then by (path, line); `more_evidence` holds the rest. Each carries `evidence_input`: `tauceti_roadmap` (a pointer into the pin, `evidence_public: true`) or `atlas_embedded` (`{evidence_input, evidence_public: false, atlas_sha256, source_path, path, line}`: that line of the atlas's own README copy, which no reader can follow to a public commit) |
| `evidence_public` | `true` when `evidence` is a `tauceti_roadmap` pointer. A row with `false` rests only on the atlas's copies: keep it out of anything presented as a public citation. When a later TauCetiRoadmap pin carries the same line, the next build verifies it at the pin first and the row turns `true` on its own |
| `via` | `atlas`; `atlas_kinds` lists the atlas edge kinds merged into the row |

**There is one direction convention**, the Brain's: consumer → prerequisite. The atlas writes
the opposite (`atlas_direction`: its `source` is the prerequisite and its `target` the
consumer); the build flips every edge, and only rejected `edge` rows keep the atlas's raw ids,
as `atlas_source`/`atlas_target`.

Candidates are the atlas's `edges` and `stageEdges`. An evidence item is verified first at the
TauCetiRoadmap pin and, if that fails, at the same line of the README copy the atlas embeds
under the evidence's own `sourcePath` string (exact key; nothing is searched or relocated), by
the same test. It is verified only if: its file exists and is the README of one endpoint's roadmap; its line exists;
the line holds the atlas's `match` text when the atlas gives one (the atlas's `sourceLine` then
points at the heading, so the lines of its quoted block are searched for the match); and the line
**names the other endpoint's roadmap**: its folder's last component appears with no letter or
digit either side (a relative link to the folder satisfies this; `GammaRay` does not name
`Gamma`). When two roadmaps share that last component (`Completed/IntegralLattices` and
`TauCetiRoadmap/IntegralLattices`), only the full folder path or a markdown link that resolves,
relative to the README, into that folder counts. The atlas's evidence paths are mapped: `content/tau-ceti/X` → `TauCetiRoadmap/X` (or
`Completed/...`), `https://github.com/TauCetiProject/TauCetiRoadmap/blob/<ref>/<path>` →
`<path>`; evidence in a personal fork's URL is `unrecognized_evidence_path`. The line number is the atlas's, not searched for: a line that moved since the atlas
snapshot fails. An edge is kept when one of its items verifies. A dropped edge goes to
`rejected.jsonl` (`kind: edge`); a failed item of a kept edge goes there as `kind: edge_evidence`.
Stage edges whose evidence is only "README order" or reviewer quotes have no line and are
rejected (`no_evidence_line`). The same rule applies within one roadmap, so an intra-roadmap
stage edge is kept only if its line names the roadmap's own folder.

**Why there are no layer → layer edges.** Every atlas stage edge is rejected, for one of two
reasons:
- its endpoint is outside the pin (`endpoint_not_at_pin`): a proposed roadmap, or an atlas stage
  that maps to no layer here;
- its evidence carries no line (`no_evidence_line`): "README order", which is sequence and
  appears as `ordinal`, or a reviewer's quote.

The atlas's embedded README copies do not help. Its evidence cites
`https://github.com/TauCetiProject/TauCetiRoadmap/blob/…` URLs, while the copies are keyed by
`content/tau-ceti/…`, so the exact `sourcePath` key never matches (7 roadmap edges verify there).
Matching the copies by the repo path both strings name would recover only 17 more edges, none
of them public, at the cost of a path-mapping rule, so it is not done. The rejected rows keep
their reasons. Layer-level dependencies need line-level evidence in the roadmaps themselves.

### `decl_refs.jsonl`

One row per (layer or roadmap, Lean name).

| Field | |
|---|---|
| `id` | `<layer id or roadmap id>::<name>` |
| `name`, `roadmap`, `layer` (`null` for a roadmap-level name) | |
| `resolution` | `mathlib`, `tauceti`, `unqualified` or `planned` (never formalized nor planned: `unqualified`) |
| `resolved_by` | input names (`mathlib_decls`, `tauceti_decls`), keys of the manifest's `resolved_by`, which holds each one's path, sha256 and pin; `null` otherwise |
| `via_namespace`, `resolved_name` | only on a name resolved through a declared scope: the scope and the full name |
| `candidates`, `candidates_total`, `candidate_libraries` | only on `unqualified`: the sorted full names (at most 8), how many, and their libraries |
| `stated`, `stated_reason` | only on `tauceti`: `true` (its proof is `sorry`) or `false` from trough's `deps.json`, only when trough's `source.json` `rev` equals the Tau Ceti `docs_commit`; else `null` with `docs_commit_unknown`, `commit_mismatch` or `not_in_deps` |
| `evidence` | pointer to the first occurrence; `occurrences` counts them |

Names come from (1) inline code spans in a README, outside fenced blocks, whose whole content
is one dotted Lean name (`Foo.bar`, segments not starting with a digit; a leading `_root_.` is
dropped); and (2) declarations in the roadmap's `Suggested.lean` (`theorem`, `lemma`, `def`,
`abbrev`, `structure`, `class`, `inductive`, named `instance`, `axiom`, `opaque`, after any
attributes and modifiers), with `namespace X` ... `end` prefixed, `section` scopes adding
nothing (attributes and `public`/`private`/`noncomputable` allowed before `section`), `end A.B`
closing one scope per component, universe parameters (`.{u}`) not part of the name,
`_root_.` making a name absolute, and comments and string literals skipped.

Attachment: a README line belongs to the innermost layer whose section holds it (a section runs
from its heading to the next heading of the same or a higher level); lines outside every layer
belong to the roadmap. In `Suggested.lean` a name belongs to the nearest preceding marker, a
markdown heading inside a `/-! ... -/` module doc; a marker whose key is a unique README layer
key gives that layer, any other marker gives the roadmap (a marker key with no README layer is
also logged as rejected).

Resolution is against the `declarations` keys of both `.bmp`s, indexed once per build (every
non-final name component; entries by last component). After the rejections below, in order:
1. **Exact**: an entry → `mathlib` (Mathlib first) or `tauceti`.
2. **Declared scope**, Lean's own resolution: the roadmap's scopes are every `namespace N`
   (full nested name) and `open N ...` / `open N in` (as written, several names allowed) in
   `Suggested.lean` and in README fenced code blocks, each with its parent prefixes. Exactly one
   entry `N.n` → its library, with `via_namespace: N`. Several → `unqualified`.
3. **`local_term`** (rejected): a dotted name whose first segment is neither a namespace
   component of any entry (read as "any non-final component", so it never hides a suffix match)
   nor a namespace the roadmaps declare (`hf.ne'`). The roadmaps' declared namespaces are taken
   across all roadmaps: every component of a declared scope (step 2) and every non-final
   component of a name `Suggested.lean` declares. A name under such a namespace that no library
   has yet is a planned declaration in a planned namespace, and stays `planned`. A namespace
   named only in README prose is not declared, so its names stay `local_term` (for example
   `BarsottiTate.*`, `Prismatic.*`, `Perf.*` at run 1's pins) until a roadmap declares the
   namespace or a reviewed proposal promotes them.
4. **`unqualified`**: the name is an entry with whole leading components removed.
5. Otherwise `planned`.

Declarations `Suggested.lean` makes are full names and resolve only exactly (step 1). No
similarity or case-folding, and no partial components: `Nat.Succ_le` and `Nat.succ_le_iff`
stay `planned` next to a real `Nat.succ_le`, and `ucc_le` is no suffix of it.
Rejected rather than `planned`: a name ending in a file suffix (`Foo.lean`), an exact module
name in either `.bmp`'s `modules`, an unmatched `Mathlib.*` name, the roadmap repo's own
modules (`TauCetiRoadmap.<Folder>[.Suggested]`), and `local_term`. Rejected rows keep their
candidate name.

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
rejected as a year, and a token not starting with `0` as `not_stacks_tag_shape`: every Stacks
tag issued so far starts with `0`, and this drops citation keys (`GK02`, `BMS1`) and line
numbers (`4197`) sharing a line with the keyword. Revisit if the Stacks project passes `0ZZZ`. **arXiv**: on a line that says `arXiv` (any case), `yymm.nnnn` (to 1412) or
`yymm.nnnnn` (from 1501), month 01-12, from 0704 on, version dropped; any other shape is
rejected as `invalid_arxiv_id`, and old-style ids (`math/0601001`) as `old_style_arxiv_id`.

### `rejected.jsonl`

Every dropped candidate: `kind` (`coverage`, `edge`, `edge_evidence`, `decl_ref`, `xref`,
`lean_marker`, `roadmap`), `reason`, what was dropped (`candidate`, `atlas_source`/`atlas_target`
for an `edge` in the atlas's direction, `source`/`target` for `edge_evidence` in ours,
`layer_key`) and a pointer where there is one. `id` is `<kind>:<first 20 hex of the sha256 of the
canonical row>`; identical rejections are one row with `count`.

### `manifest.json`

`schema`, `producer` (name, version), `inputs` (each pin: commit or sha256, URL, `fetched_at`,
and the extra facts above), `resolved_by` (by input name: path, sha256, pin), `files` (rows,
bytes, sha256 of each JSONL), `counts` (layers by state, decl refs by resolution, Tau Ceti refs
by `stated`, xrefs by system, rejections by `kind/reason`), `clock_fields`,
`attribution`, and `logical_root`: the sha256 of the canonical JSON of the manifest without
`logical_root` and with every `fetched_at` removed. A refetch of identical bytes therefore keeps
the root. The atlas's `exported_at` is part of its pinned bytes, not a clock of ours, and stays in.

## Counts at run 1's pins (2026-11-19)

Inputs: TauCetiRoadmap `96a11793`, trough `1ae5ca9f`, the atlas, Mathlib and Tau Ceti `.bmp`s of
2026-11-18. `logical_root` `e81cdc6ba03e9c54d3df48ef18d304bf88fcad3b87b985e02e1a0dd8ae95acb9`.

| File | Rows | Bytes |
|---|---|---|
| `roadmaps.jsonl` | 465 | 155,610 |
| `layers.jsonl` | 3,717 (done 487, partial 589, untouched 406, unassessed 2,235) | 2,333,439 |
| `edges.jsonl` | 2,348 (all roadmap → roadmap; 2,341 `evidence_public: true`, 7 `false`, verified only at the atlas copy; pointers: 4,267 `tauceti_roadmap`, 7 `atlas_embedded`) | 1,739,737 |
| `decl_refs.jsonl` | 77,967 (mathlib 24,608, tauceti 27,620, unqualified 2,235, planned 23,504) | 44,543,559 |
| `xrefs.jsonl` | 4,930 (stacks 2,648, arxiv 2,282) | 2,270,981 |
| `rejected.jsonl` | 22,535 rows (`decl_ref/local_term` 6,263 occurrences, 4,303 distinct names) | 8,663,133 |

All 27,620 `tauceti` rows have `stated: null` (`docs_commit_unknown`: `PINS.txt` records no docs
commit). Edges: 609 `line_does_not_name_target` remain, and no layer → layer edge survives (they
fail on endpoints or have no line).

## Licence

TauCetiRoadmap and Tau Ceti are Apache-2.0 (TauCeti contributors); tauceti-explorer is
Apache-2.0; Mathlib is Apache-2.0. The manifest's `attribution` block says so. The export
stores pointers, not prose: roadmap and layer titles and keys, coverage states, Lean names,
Stacks tags and arXiv ids are copied; README sentences, `STATUS.md` prose, the coverage comment's
`remaining` text, the atlas's evidence text and docstrings are not. A test checks this on the
fixtures.

## Not yet

- No importer: the export is not loaded into D1 or the Brain. The section below proposes how
  the Brain would carry it; the slice's table layout is for the D1 work
  ([`BRAIN-D1.md`](BRAIN-D1.md)) to fix.
- No scheduling: nothing runs `fetch` or `build` on a cadence.

## Proposed Brain schema for plan nodes (not yet in brain/SCHEMA.md)

A proposal, 2026-11-19. Nothing builds or serves it yet, and `brain/SCHEMA.md` stays the
contract until this section moves there (the last subsection says when). The vocabulary is
`brain/SCHEMA.md`'s: node types, the `contains` tree, edge records
`{kind, provenance, confidence, evidence}` and version pins.

### Nodes

A new node type, **`plan`**, with two kinds:

| `plan_kind` | id | from |
|---|---|---|
| `roadmap` | the export's id, `tauceti:<folder path>` | `roadmaps.jsonl` |
| `layer` | the export's id, `<roadmap id>#<anchor>` | `layers.jsonl` |

The ids are the export's, unchanged, so a plan node, its `/brain#<id>` address and the export
row it came from share one key. `tauceti:` is a prefix no other node type uses (Tau Ceti
declarations are `decl:TauCeti:*`, their folders `path:TauCeti/*`).

**Placement in `contains`.** Plan nodes join the one strict single-parent tree, mechanically
derived from the export and never inferred:

```
path:Plans/TauCeti/<arXiv group>  →  tauceti:<top-level roadmap>  →  <roadmap id>#<anchor>
                                     tauceti:<roadmap>  →  tauceti:<nested roadmap>  →  its layers
```

- The **arXiv group container** is a `container` node, `path:Plans/TauCeti/<topic>`, where
  `<topic>` is the roadmap's `topic` from `metadata.toml` (an arXiv category, e.g. `math.NT`). A
  top-level roadmap whose `topic` is `null` goes under `path:Plans/TauCeti/unclassified`.
- The root is source-scoped: `path:Plans` holds one child per planning source, and Tau Ceti's is
  `path:Plans/TauCeti`, so later planners sit beside it. The later PR reserves the `Plans`
  root in `brain/SCHEMA.md`, so that no library's module path can ever claim `path:Plans`;
  nothing reserves it today.
- A nested roadmap's parent is its export `parent`, never its own `topic`, which is not
  inherited (a nested roadmap with a different `topic` still sits under its parent roadmap).
- A layer's parent is its `roadmap`. Layers have no children.

**Fields carried from the export**, in the node payload:

| Field | On | |
|---|---|---|
| `label` | both | the export's `title` |
| `origin`, `topic` | roadmap | as exported |
| `key`, `state`, `ordinal`, `status_source` | layer | as exported; `ordinal` is sequence and makes no edge |
| `source` | both | the export's pointer `{repo, commit, path, line}` |
| `atlas_url` | layer | a deep link, `https://taucetiproject.org/atlas/#view=roadmap&id=<roadmap id>&layer=<atlas_id, else the layer's anchor>` |
| `planned_names`, `unqualified_names` | both | below |

We link to the atlas and never copy it: no atlas text, evidence quote or layout enters a node.
Layer `state` is the coverage claim the roadmap makes about itself, and it never feeds
formalization status (`f` bits 4 and 5). Plan nodes would take a new `f` bit, `is plan`: bit 16,
the first free one (`brain/SCHEMA.md` uses bits 0–15).

**Planned and unqualified names are attributes, never nodes.** A `planned` name is a
declaration that does not exist anywhere, and an `unqualified` name is ambiguous; neither
passes the existence oracle (design law 1). They stay on the node that cites them:
`planned_names` is the sorted list of names, and `unqualified_names` is
`[{name, candidates, candidates_total, candidate_libraries}]`, the candidates exactly as the
export gives them (at most 8). A candidate never becomes an edge until a reviewed proposal
chooses it.

### Edges

Every edge is a full edge record. `provenance` is
`{"source": "tauceti_plans", "method": <per kind>, "pin": <the export's logical_root>}`;
`tauceti_plans` becomes a key of `catalog/data/source_registry.json` in the later,
release-affecting PR that moves this section into `brain/SCHEMA.md`.

| kind | src → dst | confidence | evidence |
|---|---|---|---|
| `depends` | roadmap → roadmap, consumer → prerequisite, exactly as `edges.jsonl` emits it | `high` | the export's `evidence`, `evidence_public`, `more_evidence`, `atlas_kinds` |
| `mentions` | layer (or roadmap) → `decl:Mathlib:<n>` or `decl:TauCeti:<n>` | `high` | `{role: "plan", resolution, via_namespace?, resolved_name?, occurrences, pointer}` |
| `xref` | layer (or roadmap) → `xref:stacks:<TAG>` | `high` | `{system, occurrences, pointer}` |
| `cites` | layer (or roadmap) → `lit:<arXiv id>` | `high` | `{system, occurrences, pointer}` |

Two of these widen an endpoint type `brain/SCHEMA.md` states more narrowly today: it declares
`depends` **decl → decl** and `mentions` **concept → decl**, and a plan node is neither. The
later PR that moves this section into `brain/SCHEMA.md` widens those two rows; until it lands,
plan endpoints are not allowed there.

- **`depends`.** The direction is the Brain's, which is the export's; the build never flips
  it. Only a row with `evidence_public: true` becomes an edge. Showing an edge whose evidence
  a reader cannot follow is a claim they cannot check. So a `false` row (7 today) stays in the
  export, and the build holds it with reason `evidence_not_public` (below) until a later pin
  gives it a public pointer. Method `atlas-verified-line`.
- **`mentions`** is built from `decl_refs.jsonl` rows with `resolution` `mathlib` or `tauceti`
  only. `<n>` is `resolved_name` when present, else `name`. It is the existing citation kind
  and, like annotation citations, is excluded from all formalization-status logic: a roadmap
  naming a declaration claims nothing about it. Method `exact` or `declared-scope`.
- **`xref` and `cites`** are both built from `xrefs.jsonl`, routed by the row's `system`:
  a Stacks-family system gives `xref`, which `brain/SCHEMA.md` defines as a cross-reference to
  an external database page or tag, and `"system": "arxiv"` gives `cites`, which is the kind
  whose dst is literature. The split needs no information the export does not already carry.
  A `lit:` target is the paper node, never a statement node. Method `pattern-scan`.
- The source is the row's `layer`, or its `roadmap` when `layer` is `null` (a roadmap-level
  name or citation).

### The held-edge rule

Most `mentions` today point at declarations the Brain has not minted: it has about 7.9k of the
27,620 resolved Tau Ceti names, and many `xref:stacks:` targets of `xref` and `lit:` targets
of `cites` are not nodes either. The build never drops such an edge silently.

- An edge whose endpoint node does not exist in the build goes to a **held list**,
  `brain/data/held_edges.jsonl` (gitignored, rebuilt like `edges_links.jsonl`), as
  `{edge, reason, missing, logical_root}`: the full edge record, the reason, the missing
  endpoint id (if any) and the export's `logical_root`. There are two reasons:
  - `endpoint_missing`: an endpoint node does not exist;
  - `evidence_not_public`: a `depends` row with `evidence_public: false`.
- Every build retries every held edge, and emits it into the edge files as soon as its reason
  no longer holds: both endpoints exist, or a public pointer verifies at the pin. Holding is
  per build, so no state carries over except through the inputs.
- Each release records the held list's count by kind and reason, and its digest, in its
  release evidence (step 4's release contract). `test_acceptance` checks, by kind and reason:

      previous held − emitted this build + newly held = current held

  It refuses the build if a held edge disappears without being emitted, unless its export row
  is gone too (a new `logical_root` that drops it).

### Where the inputs come from

`build_snapshot` takes the pinned export as one more input under `--data-dir`, as
`tauceti_plans/` holding the six JSONL files and `manifest.json` unchanged. It checks every
file's sha256 against the manifest and recomputes `logical_root` before reading a row, and
refuses on any mismatch. A release pins the export by `logical_root` and records it in its
manifest next to the other input pins. The export itself is never committed (it is about
60 MB and growing); it is staged from a private store, like the D1 snapshot and Wikidata
bundles. No export, no plan nodes: the build then omits them and says so, never failing on a
missing optional input and never inventing one.

### Serving on `wikilean-brain`

No new table is expected. The plan nodes map onto the section-4 tables of
[`BRAIN-D1.md`](BRAIN-D1.md) as follows:

| Table | Carries |
|---|---|
| `labels` | one row per plan node, `p` = its `contains` parent, `f` with the plan bit, `libs` `NULL` |
| `cell_pages` | each plan node's page JSON: the payload fields above and its edges, with `depends` evidence pointers, `mentions`, `xref` and `cites` lists |
| `supercells` | `path:Plans`, `path:Plans/TauCeti` and the `path:Plans/TauCeti/<topic>` containers, and roadmaps that have children, so the breadcrumb and `kids` work as for any folder |
| `name_keys` | titles as `label`; layer keys (`Layer 5`, `R2.3`) as `aka` |
| `aliases` | the atlas id of a layer whose `atlas_id` differs, mapped to our id, so an atlas link resolves |
| `xrefs` | the `xref` and `cites` edges, page → node, so `xref:stacks:` and `lit:` pages list the layers that cite them |
| `synapse_traces` | the `depends` and `mentions` pairs, with their evidence |

Deferred to the build-side PR, and decided there together with the per-source-dataset
question (Jack's decision), so that `migrations-brain/0002` is written once. Two candidates:
- the held list served on D1 (an agent asking "which plans name this missing declaration?");
  it would be a new table, or it stays local and is never served;
- a column for `plan_kind` or `state` on `labels`, if the filter must select layers by state
  without reading `cell_pages`. Both would be additive, under section 4's schema rule.

### Counts at the current export

Root `eea7834b…` (a later export than run 1's above):

| | |
|---|---|
| roadmaps | 466 |
| layers | 3,722 |
| `depends` | 2,348 export rows: 2,341 become edges, 7 are held (`evidence_not_public`) |
| resolved `mentions` candidates | 52,245 (mathlib 24,625, tauceti 27,620) |
| xrefs | 4,932 |

### What moves later, and when

After the code-only release, this section moves into `brain/SCHEMA.md` in one
release-affecting PR, together with the `build_snapshot` reader, the held list, the acceptance
check and the `source_registry.json` key. Until then, `brain/SCHEMA.md` is not touched, because
the build may read it.
