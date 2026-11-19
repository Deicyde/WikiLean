# Brain step 0: the measurement harness

The full-size measurement build of `docs/BRAIN-D1.md` section 8, step 0, repeating the 11-06
method of `docs/BRAIN-TAUCETI.md`: take the tracked Brain graph in `brain/data/` (the 08-28
build), mint its TauCeti layer again from a fresh harvest with `build_common`'s own frontier
layer, then run the real cells, layout (#137 grid and module bubbles), shard and serving-database
stages, and measure the result. Local only: no D1, no wrangler.

Two scopes:
- **A**: every harvested TauCeti declaration a cell.
- **B**: the hybrid. The TauCeti layer keeps the union of trough's mapped declarations
  (`deps.json`, `selection.json`, the names in `roadmaps.json`), the roadmap-referenced
  `tauceti` names of the plans export's `decl_refs.jsonl`, and the current Brain's TauCeti
  declarations still in `tauceti.bmp`; the serving file also gets a prototype name index
  (`names.py`) over both doc-gen4 indexes.

The three input directories, none of which the harness fetches (acquire them first, and nothing
below touches the network):

| Option | What it names | Where it comes from |
|---|---|---|
| `--tauceti` | a full (or shallow, but not partial) clone of `TauCetiProject/TauCeti`, the repo `lean_repo.SPECS["tauceti"]` names, with its `TauCeti/` Lean tree | `git clone` |
| `--inputs` | the Tau Ceti plans **acquisition** directory: `decls/mathlib.bmp`, `decls/tauceti.bmp` (the doc-gen4 indexes) and `trough/data/tauceti/{deps,selection,roadmaps}.json` | `python3 -m scripts.tauceti_plans fetch --inputs <dir>` — the only networked step, and the one place pins are taken (`docs/BRAIN-PLANS.md`) |
| `--plans` | the plans **export**, for its `decl_refs.jsonl` | `python3 -m scripts.tauceti_plans build --inputs <that dir> --out <dir>` (offline; `docs/BRAIN-PLANS.md`) |

Scope A needs only `--tauceti`; scope B needs all three.

```bash
# heavy steps under a memory cap; numpy is needed by brain/layout.py
systemd-run --user --scope -p MemoryMax=64G nice -n 10 \
  python3.12 scripts/brain_measure/measure.py run --scope B --out ../out \
    --tauceti ../work/tauceti --inputs ../inputs --plans ../plans-export
```

`run` executes each stage (`harvest select graph cells layout shards serving nameindex
analyze`) as its own process and records wall time and peak memory in
`<out>/<scope>/timings.json`; `--stages` picks a subset. Stage facts go to `facts.json` and the
measurements to `analysis.json` beside it. Nothing is written inside the checkout.

- `measure.py`: the stages. The harvest lifts `lean_repo.DECL_CAP` in its own process only.
  `git_snapshot` refuses a partial clone, so point `--tauceti` at a full (or shallow) clone.
- `names.py`: the scope-B name index (`decl_names` plus an FTS5 trigram table).
- `report.py`: collects both scopes into one `results.json` and prints the A-against-B table.
- `analyze.py`: `dbstat` sizes, the verify-style digest time (`open_local(full_check=True)`),
  query times, and the offline import plan: the real `site/ops/brain_d1_import.py` importer
  run against a local SQLite stand-in for D1 created from `wiki/migrations-brain/0001_brain.sql`.
  The stand-in's file after the import is the projected D1 footprint.

Never commit build outputs or measurements. Tests: `test_brain_measure.py` (offline, in
`scripts/ci-python.sh`).
