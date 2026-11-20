# Brain plans: the Tau Ceti plan producer (`wikilean.brain.plans/v1`)

Contract, 2026-11-18. Code: `scripts/tauceti_plans/` (release-neutral). Since 2026-11-20 its
export feeds the Brain's plans layer (`brain/SCHEMA.md`, "Plans layer"; section "In the Brain"
below), so a change to the export's shape is a change to a release input. Context: [`BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md)
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

- No scheduling of producers 2 and 3, and no switch of the Brain's Tau Ceti layer to them
  ([`BRAIN-PRODUCERS.md`](BRAIN-PRODUCERS.md)); that change lands with
  [`BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md)'s step 8.
- No plan cells on `/brain` and nothing on D1 yet (below).

## In the Brain

The plan-node schema this section proposed on 2026-11-19 is now `brain/SCHEMA.md`'s **Plans
layer**, built by `brain/plans_layer.py`: plan nodes and their `path:Plans` containers (the
`Plans` root reserved), facet bit 16, the `depends`, `mentions`, `xref` and `cites` edges, the
held list (`brain/data/held_edges.jsonl`, reasons `endpoint_missing` and
`evidence_not_public`) and its accounting check (`test_acceptance` P17). The nightly runs this
producer weekly and stages its export (`site/ops/brain_producers.py`, below). That contract
is authoritative; three things differ from the proposal:

- **Live builds only.** Only the nightly's `reducer-inputs-v1.json` declares the export. The
  replay inventories do not, so a replay has no plan nodes and replays byte for byte; the next
  replay inventory (the D1 one, [`BRAIN-D1.md`](BRAIN-D1.md)) adds it.
- **No `atlas_url`.** The atlas's deep links name its own roadmap and stage ids
  (`#view=roadmap&id=SchemeAndStackFoundations&layer=SchemeAndStackFoundations%3ASF.5`), which
  the export does not carry, so a link built from our ids would not resolve. It waits for the
  producer to export the atlas's ids.
- **Plan cells come later.** `build_cells` makes cells from concepts and declarations only, so
  plan nodes reach `nodes.jsonl`, `edges.jsonl` and the SQLite index but not the page, where
  `path:Plans` is a root with no cells. Cells, synapses and the facet chip are a separate
  change, as is the D1 side: the held list served or not, and a `plan_kind`/`state` column on
  `labels`, decided with `migrations-brain/0002`.

### Running the producer in the nightly

`site/ops/brain-nightly.sh` runs `site/ops/brain_producers.py` with its weekly sources:

```bash
python3 site/ops/brain_producers.py run tauceti_plans --repo REPO --store STORE
python3 site/ops/brain_producers.py stage tauceti_plans --store STORE --into REPO/brain/data/tauceti_plans
python3 site/ops/brain_producers.py check tauceti_plans --store STORE --max-age-hours 504
```

`run` fetches into `STORE/inputs/<producer>/` (the only networked step), builds into a fresh
folder of `STORE/work/`, and publishes `STORE/exports/<producer>/<logical_root>/` only when the
root is new, with `PRODUCER.json` (code commit, interpreter) and `PINS.json` beside the export;
`current` then points at it, and the newest exports (default 8), `current` and any root listed
in `PINNED` are kept. Every run writes `last_success.json` or `last_failure.json`. `stage`
copies exactly the export's seven files into `brain/data/tauceti_plans/` when its root differs
from the staged one, after the same checks the build makes (`load_export`, then the layer's
structure checks), and swaps the directory in; a staged export that fails them is moved aside
(`.tauceti_plans.invalid-*`) so the build goes on without plans rather than refusing. `run`
makes the same checks before it publishes. `check` exits 1 when the producer never succeeded, last succeeded too long
ago, or failed after its last success. `STORE` must be outside every git work tree (the
producer refuses an `--out` inside one): the default is
`~/.local/share/wikilean/brain-producers`, overridable with
`WIKILEAN_BRAIN_PRODUCER_STORE`. The step is fail-soft, like the other ingest adapters: on any
failure the staged export stays as it was, the build reads it, and the log says so.

### Counts at the current export

Root `eea7834b…` (a later export than run 1's above):

| | |
|---|---|
| roadmaps | 466 |
| layers | 3,722 |
| `depends` | 2,348 export rows: 2,341 become edges, 7 are held (`evidence_not_public`) |
| resolved `mentions` candidates | 52,245 (mathlib 24,625, tauceti 27,620) |
| xrefs | 4,932 |
