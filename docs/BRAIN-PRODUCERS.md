# Brain producers: the shared convention, three producers, `check_pins`

Contract, 2026-11-19. Code: `scripts/brain_producers/` (shared), `scripts/tauceti_plans/`
(producer 1, contract [`BRAIN-PLANS.md`](BRAIN-PLANS.md)), `scripts/tauceti_decls/` (producer 2),
`scripts/mathlib_decls/` (producer 3). Release-neutral: nothing in the Worker, the public assets or
a Brain release reads them yet. Context: [`BRAIN-D1.md`](BRAIN-D1.md) (one producer per source, each
a sealed, pinned export that becomes one D1 serving-dataset slice).

## The convention

- **`fetch` is the only networked step.** It pins every input by commit or sha256 and writes
  `PINS.json`; with `--pins` it fetches exactly those commits and bytes and exits 1 naming the
  input on any mismatch, never writing the mismatched bytes and leaving no `PINS.json`. Every
  network call goes through one `Opener` (producer 1's, `scripts/tauceti_plans/fetch.py`), which
  the tests replace with an offline fake.
- **`build` is offline and deterministic.** It recomputes every pin and refuses (exit 1) one that
  differs, reads git inputs from the pinned commit (`GitTree`, so a dirty tree cannot leak in),
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
`fetch`, run producer 2's `fetch` (or producer 1's on a new directory) before building producer 1.
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
`stated_reason` `docs_commit_unknown`, `commit_mismatch`, `deps_rev_mismatch`, or `not_in_deps`
for a mapped declaration trough has no record of. The manifest's `stated` block records the two
commits and the reason. When trough places a declaration in another module than the `.bmp`,
`line`, `signature`, `doc` and `source` are null (`decl/module_mismatch`).

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
  next line) in overview/undergrad. A URL or prose value is `list_entry/not_lean_name`; a
  `Mathlib/…/X.html` value is a module, `list_entry/module_entry`.
- Edges come from `concepts`' `statement` and `proof` lists, as in producer 2.
- The manifest carries `mathlib_commit` (trough's `source.json` rev), `docs_commit` (the `.bmp`'s)
  and `mathlib_commit_matches_docs` (null when either is unknown).

## `check_pins`

`python3 scripts/brain_producers/check_pins.py EXPORT_DIR...` reads each manifest and, for every
pin two or more exports carry, checks they agree: `trough.commit` (producers 1–3),
`tauceti_decls.sha256` and `.docs_commit` (1 and 2), `mathlib_decls.sha256` and `.docs_commit`
(1 and 3). A null pin disagrees with a set one. It prints one line per pin, exits 1 naming every
mismatch and 2 when a manifest is unreadable or not a producer's.

## Licence

TauCeti and Mathlib are Apache-2.0; trough is Apache-2.0. Producers 2 and 3 copy declaration
names, modules, kinds, lines, signatures and docstrings, and say so in `attribution`. List titles,
authors and notes from the yaml are not copied.

## Not yet

- No importer into D1 or the Brain, and no schedule.
