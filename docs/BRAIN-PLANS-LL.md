# Tau Ceti plans as Literature Links

Contract, 2026-11-19. Code: `scripts/tauceti_plans_ll/`. Release-neutral: nothing in the
Worker, the public assets or a Brain release reads it. It turns a sealed Tau Ceti plans export
([`BRAIN-PLANS.md`](BRAIN-PLANS.md)) into a folder of
[Literature Links](https://github.com/mathlib-initiative/literature-links) annotations (format
1.x, spec at commit `d3d7c2c6`, Apache-2.0). Each annotation links a roadmap layer, or a whole
roadmap README, to Lean declarations.

## Command

```bash
python3 -m scripts.tauceti_plans_ll build --export DIR --inputs DIR --out DIR
python3 scripts/tauceti_plans_ll/test_build.py
# validate (the checker is not a WikiLean dependency; CI does not run it)
PYTHONPATH=<literature-links>/src python3 -m literature_links scan OUT/annotations --root OUT/annotations
```

Python 3.12, standard library only. `--export` is the sealed export and `--inputs` holds the
inputs it was built from (`TauCetiRoadmap/`, `trough/`, `decls/`). `build` runs `git` locally to
read the two checkouts and nothing else.

Before it reads a row, `build` checks every export file's sha256 and row count against
`manifest.json`, recomputes the export's `logical_root` and checks that the schema is
`wikilean.brain.plans/v1`. It then checks the inputs against the export's pins: both HEAD
commits, the sha256 of trough's `data/tauceti/roadmaps.json` (read from the commit, never the
working tree) and of both `.bmp` files. Each layer's heading must be at its exported line with
its exported title. Any mismatch exits 1 with `build failed: …`.

**The output is never committed.** `build` refuses an `--out` inside a git work tree (any
ancestor holding `.git`), and an `--out` that exists and is not an empty directory, so that
stale files cannot be scanned with new ones.

## Layout

```
OUT/manifest.json
OUT/annotations/_defaults.json
OUT/annotations/TauCetiRoadmap/<Name>.jsonl
OUT/annotations/Completed/<Name>.jsonl
```

One JSONL file per top-level roadmap folder; a nested roadmap
(`TauCetiRoadmap/RepresentationTheory/AdoIwasawa`) is in its top-level roadmap's file. The
annotations sit in a folder of their own, as the checker's guide asks, because `scan` reports
every JSON file it walks that is not an annotation, and `manifest.json` is not one.

Every line is `scripts.brain_producers.common.dumps` (sorted keys, no NaN) and LF. Within a
file, annotations go by roadmap id, then by subject id (the roadmap before its layers), then
unlinked, `hasPart`, `relatedMatch`, then the canonical bytes of the object. Two builds of the
same inputs are byte-identical.

`_defaults.json` holds the subject template that every annotation inherits:

```json
{"subject":{"type":"generic","uri":"https://github.com/TauCetiProject/TauCetiRoadmap","version":"<the export's TauCetiRoadmap commit>"}}
```

`manifest.json` holds `schema` (`wikilean.tauceti_plans_ll/v1`), `producer`, `source_export`
(the export's schema, producer and `logical_root`), `inputs` (the commits and sha256s checked
above and each `.bmp`'s `docs_commit`), `files` (rows, bytes and sha256 of each file),
`counts`, `attribution` (the export's, plus the format's) and `logical_root`, the sha256 of the
canonical manifest without it. There is no clock field.

## Mapping

| Export | Literature Links |
|---|---|
| a layer (`layers.jsonl`) | a subject end: `generic`, location `{"path": <README path>, "lines": [start, end]}`, `title` the layer title; `id` the layer id |
| a roadmap (`roadmaps.jsonl`) | a subject end with location `{"path": <README path>}` (the whole README), `title` the roadmap title; `id` the roadmap id; no `status` |
| layer `state` | `status`: `done` → `formalized`, `partial` → `partial`, `untouched` → `not_formalized`, `unassessed` → none |
| a layer with no link | one annotation with `"object": []` and no `relation`, which keeps its `status` |
| `decl_refs` row, resolution `mathlib` or `tauceti` | `relatedMatch` to a `lean` end: `uri` the Mathlib or Tau Ceti repository, `version` that `.bmp`'s `docs_commit`, location `{"declaration": <resolved_name, else name>}` |
| `decl_refs` row from a declaration in the roadmap's `Suggested.lean` | `hasPart` to a `lean` end in TauCetiRoadmap at the export's commit, location `{"declaration": <full name>, "module": <module of Suggested.lean>}` |
| a name in trough's `roadmaps.json` for a roadmap | `hasPart`, roadmap → a `lean` end in the library whose `.bmp` has the name as an entry (Mathlib first) |
| `planned` and `unqualified` names; trough names no `.bmp` has | names only, under `extensions["org.wikilean.tauceti_plans"]` (`planned`, `unqualified`, `unresolved_results`) |
| `edges.jsonl`, `xrefs.jsonl` | left out; they stay in the Brain export |

**The subject location** is producer-defined, as every `generic` location is: `path` is the
README's path in the repository at the end's `version`, and `lines` is `[start, end]`, 1-based
and inclusive, counted as the spec's `lines` (LF, CRLF or CR ends a line; a README with a bare CR
is refused, since the export numbers lines by LF). A layer's section runs from its heading to the
line before the next heading of the same or a higher level (any heading, layer or not, outside
code fences, by the export's own heading parser), or to the last line of the file. So a section
holds its subsections, nested layers included. A roadmap subject has `path` alone.

**One annotation per layer, always**, and one more for each link: independent links are
separate annotations, as the spec asks, each with the layer's `id` and `status`. A roadmap gets
annotations only when it has a link or omitted names (an unlinked one then carries them).

**`relatedMatch`.** A README naming a declaration is a relation, never a statement match. Two
rows of one layer whose names resolve to one declaration give one annotation. Where the
manifest gives no `docs_commit` for a library, its ends have no `version`; `counts`
`objects_without_version` says how many.

**`hasPart`, layer → its `Suggested.lean` target.** The export marks a `Suggested.lean`
declaration by its `evidence` path, the roadmap's `Suggested.lean`. The export keeps the first
occurrence by (path, line) and `README.md` sorts before `Suggested.lean`, so a declaration that
the same layer's README also names has README evidence. `build` therefore also re-scans
`Suggested.lean` at the pinned commit with the producer's parser and marker rule
(`scripts.tauceti_plans.parse.lean_scan`, a marker heading naming a unique layer key), and
takes a row whose name the file declares under the row's own owner (`counts`
`suggested_rows_from_readme_evidence`). A row with `Suggested.lean` evidence that the re-scan
does not place under that owner is refused. The declaration's owner may be the roadmap (a
marker naming no layer), and then the subject is the README. The module is the file's path
with `/` as `.` and no `.lean` (`lakefile.toml` globs `TauCetiRoadmap.*` and `Completed.+` from
the root). The statements exist in TauCetiRoadmap even when no library has them. A
`Suggested.lean` declaration whose name also resolves in a library gets both links.

**`hasPart`, roadmap → trough's results.** trough's `roadmaps.json` lists, per roadmap path, the
declarations its status names as results. A name counts only when it is exactly an entry of one
of the pinned `.bmp` indexes, the data the export resolved against (`resolved_by`): no scope or
suffix rule, Mathlib first. The rest go to `unresolved_results`. trough read the roadmaps at
its own `rev`, recorded in the manifest as `roadmaps_json_rev`; the subject is the README at the
export's commit.

**Omitted names.** One annotation of each subject, the first in file order, carries the
extension: sorted names, never candidates or prose. A `planned` name that is a `Suggested.lean`
declaration is a link, not omitted.

**No prose.** Only titles, paths, line numbers, commits and Lean names are written. A test
checks the fixtures' README, `STATUS.md` and docstring text never reach the output.

## Counts at export root `eea7834b…` (2026-11-19)

TauCetiRoadmap `890b66de`, Mathlib docs `a97078fa`, Tau Ceti docs `bf5b062a`. Output
`logical_root` `8722fbe2…`; 70,912 annotations in 431 files, 35 MB.

| | |
|---|---|
| layers | 3,722 (formalized 487, partial 589, not_formalized 406, no status 2,240) |
| unlinked layers | 1,012 |
| `relatedMatch` | 51,906 |
| `hasPart` | 17,994 (15,818 `Suggested.lean` targets, 167 of them recovered by the re-scan; 2,176 trough results) |
| unlinked annotations | 1,012 |
| omitted names | planned 7,845, unqualified 2,235, unresolved trough results 9 |

The checker reports `70912 annotations from 431 files; 0 problems`.

## Licence

TauCetiRoadmap, Tau Ceti and Mathlib are Apache-2.0; the manifest repeats the export's
attribution block and adds the format's. No Literature Links code or text is copied.
