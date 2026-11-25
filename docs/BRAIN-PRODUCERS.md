# Brain producers: the shared convention, four producers, `check_pins`

Contract, 2026-11-19; producer 4 added 2026-11-24. Code: `scripts/brain_producers/` (shared),
`scripts/tauceti_plans/` (producer 1, contract [`BRAIN-PLANS.md`](BRAIN-PLANS.md)),
`scripts/tauceti_decls/` (producer 2), `scripts/mathlib_decls/` (producer 3), `scripts/rh_decls/`
(producer 4). Release-neutral: nothing in the Worker, the public assets or
a Brain release reads them yet. Context: [`BRAIN-D1.md`](BRAIN-D1.md) (one producer per source, each
a sealed, pinned export that becomes one D1 serving-dataset slice).

## The convention

- **`fetch` is the only networked step.** It pins every input by commit or sha256 and writes
  `PINS.json`; with `--pins` it fetches exactly those commits and bytes and exits 1 naming the
  input on any mismatch, never writing the mismatched bytes and leaving its own inputs unpinned
  (`PINS.json` keeps only entries this run never touched, and goes when none are left). Every
  network call goes through one `Opener` (producer 1's, `scripts/tauceti_plans/fetch.py`), which
  the tests replace with an offline fake.
- **`build` is offline and deterministic, on every git version.** It makes no network call.
  `GitTree`, and the Literature Links converter's reads, run git with `-c protocol.allow=never`,
  which forbids every transport, and also set `GIT_NO_LAZY_FETCH=1`. That variable only exists
  from git 2.45: older git (Apple's 2.39, for one) ignores it, and only the protocol block stops
  the lazy fetch there. So a file whose blob a `--filter=blob:none` clone never fetched is an
  error naming the file, not a silent fetch from the remote. When old git exits from
  `cat-file --batch` instead of reporting the blob missing, the reader checks each blob and
  still names the missing one. It recomputes every pin and refuses
  (exit 1) one that differs **and one an input does not carry** — no pins file, or an input the
  pins do not name, stops the build rather than sealing an export nothing was checked against.
  It reads git inputs from the pinned commit (`GitTree`, so a dirty tree cannot leak in),
  and writes canonical JSONL (keys sorted, `ensure_ascii=False`, separators `(",",":")`, LF, rows
  sorted by unique `id`) plus `manifest.json`. Two builds of the same inputs are byte-identical.
- **`logical_root`** is the sha256 of the canonical JSON of the manifest without `logical_root`
  and with every `fetched_at` removed, so a refetch of identical bytes keeps the root.
- **Pointers, not prose,** except where the licence allows it, with the attribution in the
  manifest's `attribution` block.
- **`build` refuses an `--out` inside a git work tree.** Exports are never committed.
- Tests are offline, on fixtures (`scripts/brain_producers/fixtures/` for producers 2 and 3).

Shared code: `common.py` (canonical JSON, hashing, `git`, `GitTree`; `scripts/tauceti_plans/canon.py`
re-exports it), `decls.py` (pins, the name index, dependency edges, rejected rows, the seal),
`fetch_decls.py`, `cli.py`, `check_pins.py`.

```bash
python3 -m scripts.tauceti_decls fetch --inputs DIR [--pins PINS.json]
python3 -m scripts.tauceti_decls build --inputs DIR --out OUT   # likewise scripts.mathlib_decls
python3 scripts/brain_producers/check_pins.py EXPORT_DIR...
```

## Inputs and pins

All three producers may share one inputs directory (producer 1's layout). Producers 2 and 3 read:

| Input | Where | Pin |
|---|---|---|
| trough | `trough/` (sparse checkout; producer 2 needs `data/tauceti/`, producer 3 `data/mathlib/`) | HEAD commit, and each file's sha256 in the manifest |
| Tau Ceti declarations | `decls/tauceti.bmp` (doc-gen4 `declaration-data.bmp`, JSON) | sha256 and `docs_commit` |
| Mathlib declarations | `decls/mathlib.bmp` | sha256 and `docs_commit` |

Their `fetch` adds `data/<lib>` to trough's sparse set when the checkout lacks it, and replaces
only its own entries in a shared `PINS.json` (`trough` and `<lib>_decls`), keeping the others.
Producer 1's `fetch` does not widen the sparse set, so after a fresh clone by producer 3's
`fetch`, run producer 2's `fetch` (or producer 1's on a new directory) before building producer 1:
a build that needs a blob the clone does not hold fails naming the file, since it will not fetch it.
A `fetch` that moves trough changes the trough pin the other producers then build against;
`check_pins` on the exports catches exports built from different pins.

## Producer 2: `wikilean.brain.tauceti-decls/v1`

From trough's `data/tauceti/` (`deps.json`, `selection.json`, `roadmaps.json`, `source.json`) and
`tauceti.bmp`. Declaration ids are `brain/SCHEMA.md`'s `decl:<Library>:<FQ name>`, with the
index's library (`decl:TauCeti:…`).

| File | Rows |
|---|---|
| `names.jsonl` | every declaration of the `.bmp`: `id`, `name`, `module` (from the `docLink` path), `kind`, `doc_link`. No docstrings. A `docLink` of another shape is rejected (`name/doc_link_unparseable`) |
| `decls.jsonl` | the mapped declarations, deps ∪ chosen (`selection.json`) ∪ status-named (`roadmaps.json`), present in the `.bmp`: `module`, `kind` (both the `.bmp`'s), `line`, `signature`, `doc`, `source` (`{repo: TauCetiProject/TauCeti, commit: source.json rev, path, line}`), `mapped_by`, `selection` (trough's label), `roadmaps` (producer 1's roadmap ids), `stated` |
| `deps.jsonl` | `uses_in_statement` / `uses_in_proof` edges from `deps.json`'s `statement` and `proof` lists, `source` the consumer and `target` the prerequisite (the Brain's direction), both mapped; `evidence` points at trough's pinned `data/tauceti/deps.json` |
| `rejected.jsonl` | as producer 1 (`<kind>:<20 hex>`, `count`): `decl/not_in_index`, `decl/trough_missing`, `decl/module_mismatch`, `dep/target_not_mapped`, `dep/self_edge` |

`stated` is `deps.json`'s `sorry` only when `source.json`'s `rev` equals the `.bmp`'s pinned
`docs_commit` (and `deps.json`'s own `tauceti_rev` agrees). Otherwise it is null, with
`stated_reason` `docs_commit_unknown`, `commit_mismatch`, `deps_rev_mismatch`, `not_in_deps`
for a mapped declaration trough has no record of, or `module_mismatch`. The manifest's `stated`
block records the two commits and the reason (the first three are the whole export's; the last
two are per row). When trough places a declaration in another module than the `.bmp`, `line`,
`signature`, `doc` and `source` are null and so is `stated` (`decl/module_mismatch`): one record
is either trustworthy or it is not.

## Producer 3: `wikilean.brain.mathlib-decls/v1`

From trough's `data/mathlib/` (`concept-deps.json`, `yaml/{overview,undergrad,100,1000}.yaml`,
`source.json`) and `mathlib.bmp`. The same four files; ids `decl:Mathlib:…` (the index's library;
`module` says where a declaration really lives, e.g. `Init.Core`).

- Mapped: `concepts` ∪ `cited` ∪ the `decls` of the modules trough resolved ∪ every name on a
  curated list, present in the `.bmp`. Rows add `deprecated`, `lists` (`overview`, `undergrad`,
  `100`, `1000`) and `list_entries` (`{list, entry, repo, commit, path, line}` into trough's pinned
  yaml; `entry` is the 100/1000 key, a number or a Wikidata QID). `source` points into
  `leanprover-community/mathlib4` at trough's Mathlib commit, for `Mathlib`, `Archive` and
  `Counterexamples` modules only (null for `Init`, `Std`, …). No `stated`: Mathlib has no `sorry`.
- The yaml is read by a small standard-library reader of the subset those files use (`lists.py`):
  `decl:`, `statement:` and `decls:` (block or `[a, b]`) in 100/1000, leaf values (also on the
  next line, quoted or plain) in overview/undergrad. The body of a `>`/`|` block is prose, and a
  value that is only a comment is no value. A URL or prose value is `list_entry/not_lean_name`; a
  `Mathlib/…/X.html` value is a module, `list_entry/module_entry`.
- Edges come from `concepts`' `statement` and `proof` lists, as in producer 2.
- The manifest carries `mathlib_commit` (trough's `source.json` rev), `docs_commit` (the `.bmp`'s)
  and `mathlib_commit_matches_docs` (null when either is unknown).

## Producer 4: `wikilean.brain.rh-decls/v1`

From the source of
[leanprover-community/riemann-hypothesis](https://github.com/leanprover-community/riemann-hypothesis).
That repository publishes no doc-gen4 site and trough has no record of it, so this producer reads
its source at one commit with the Brain's own Lean reader: `lean_repo.harvest_rows` (the
frontier-repo harvest) on each file for the name, kind, statement and docstring, and
`build_common._lean_decl_lines` for the line. No Lean runs. The files read are the modules of the
libraries `lake build` builds: `lakefile.toml` at the commit gives `srcDir`, `defaultTargets` and
each `lean_lib`'s globs (`X`, `X.+`, `X.*`; `roots`, else the library's name, when there are none).
Comparator's `Challenge` library is not a default target. It restates the five headline theorems
with `sorry` under the same names, so reading it would make them ambiguous.

```bash
python3 -m scripts.rh_decls fetch --inputs DIR [--pins PINS.json]
python3 -m scripts.rh_decls build --inputs DIR --out OUT
```

The input is `rh/`, a clone one commit deep with only `src/` checked out (`--depth 1
--filter=blob:none --sparse`), pinned by commit in PINS.json's `rh` entry. It shares no input with
producers 1–3. Ids are `decl:RiemannHypothesis:<Lean name>` for the whole repository; `module` and
`lean_lib` say where the declaration lives.

| File | Rows |
|---|---|
| `names.jsonl` | every declaration: `id`, `name`, `module`, `lean_lib`, `kind`, `private`, `source` (`{repo, commit, path, line}`) |
| `decls.jsonl` | the same declarations, adding `source_name` (the name as written), `line`, `signature` (the statement as the frontier harvest cuts it: from the declaration line to `:=`, at most 12 lines and 700 characters), `doc` (the docstring, whitespace collapsed, at most 700 characters) and `refs` (Erdős, Wikipedia and OEIS links in the docstring) |
| `deps.jsonl` | empty: dependency edges need the elaborated project |
| `rejected.jsonl` | `decl/ambiguous_name` (a public name declared in more than one module, which Lean allows when no module imports both; neither is exported), `file/outside_default_libs` (a `.lean` file under `srcDir` that no default library takes), `file/not_utf8` |

- A `private` declaration gets Lean's name for it, `_private.<module>.0.<name>`, so two modules'
  private helpers of one name stay two declarations. Only the declaration line is read for
  `private`.
- What the source does not spell out is not exported: structure fields, anonymous instances,
  `to_additive` twins and other generated names. Commented-out code is skipped.
- The reader's files (`brain/build_common.py`, `brain/ingest/{common,formal_conjectures,
  git_snapshot,lean_repo}.py`) are hashed into the manifest's `reader`, so a change to the reader
  gives a new logical root. The manifest's `lake` block records the libraries read and left out.

Measured at main `fcb6eac7` on 2026-11-24, one process on a Linux server:

- 752,946 declarations from 59,763 files in the nine default libraries: Development 496,906,
  Formalism 166,584, GroundTheory 41,262, ReciprocityTheory 28,732, CompanionTheory 9,099,
  GroundTheoryFields 7,178, PositivityTheory 2,986, Lemmata 190, Main 9.
- 54,871 private, 346 with a docstring. Five ambiguous names and `src/Challenge.lean` rejected.
- The fetch takes about 100 s and 1.4 GB on disk. The build takes about 6 minutes and writes
  1.0 GB (`decls.jsonl` 649 MB, `names.jsonl` 399 MB).

That is about 2.4 times Tau Ceti's 311k declarations, so, like Tau Ceti, it can reach the site only
after the D1 cutover ([`BRAIN-TAUCETI.md`](BRAIN-TAUCETI.md)).

## `check_pins`

`python3 scripts/brain_producers/check_pins.py EXPORT_DIR...` reads each manifest and, for every
pin two or more exports carry, checks they agree: `trough.commit` (producers 1–3),
`tauceti_decls.sha256` and `.docs_commit` (1 and 2), `mathlib_decls.sha256` and `.docs_commit`
(1 and 3). A null pin disagrees with a set one. It prints one line per pin, exits 1 naming every
mismatch and 2 when a manifest is unreadable or not a producer's. Producer 4's exports are
accepted and carry no shared pin.

## Licence

TauCeti and Mathlib are Apache-2.0; trough is Apache-2.0. Producers 2 and 3 copy declaration
names, modules, kinds, lines, signatures and docstrings, and say so in `attribution`. List titles,
authors and notes from the yaml are not copied.

riemann-hypothesis has no licence: its README says the submission belongs to its author and was
published without one. On 2026-11-24 Jack chose to carry its statements and docstrings anyway, an
exception to "pointers, not prose" above. Producer 4's `attribution` says so, with `license`
null. The repository's `comparator/` files (Apache-2.0, from formal-conjectures) are outside `src/`
and are not read.

## Not yet

- No importer into D1 or the Brain, and no schedule.
- Producer 4 has no dependency edges, and its `signature` is the declaration's header, so a
  definition's body after `:=` is not in it. Both need more than the source text.
