#!/usr/bin/env python3
"""Brain step 0: the full-size measurement build of scope A and scope B (local only).

    measure.py run   --scope A|B --out DIR [--tauceti DIR] [--inputs DIR] [--plans DIR]
                     [--stages s1,s2,...]
    measure.py stage NAME --scope A|B --out DIR [same options]

`run` starts every stage as its own process and records its wall time and peak resident
memory (wait4) in DIR/<scope>/timings.json. The stages, in order:

  harvest    lean_repo.harvest_rows over the TauCeti checkout with DECL_CAP lifted in this
             process only (shared by both scopes: DIR/harvest/tauceti.jsonl)
  select     the TauCeti layer's declarations: all of the harvest (A), or the hybrid set (B)
  graph      the tracked Brain graph (brain/data, the 08-28 build) with its TauCeti layer
             removed and minted again by build_common._frontier_repo_layer from the selection;
             then xref_index.json by build_shards' own renderer
  cells      build_cells.build without the layout
  layout     layout.layout_cells (the #137 grid and module bubbles)
  shards     build_cell_shards.build_cell_shards
  serving    build_serving_db.build
  nameindex  B only: the doc-gen4 name index added to the serving file (names.py)
  analyze    sizes, the offline import plan, query times and the digest time (analyze.py)

Every builder is imported read-only from the checkout; nothing under brain/, catalog/, site/
or wiki/ is written. All outputs go under DIR.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BRAIN = REPO / "brain"
for _p in (BRAIN / "ingest", BRAIN, REPO / "site" / "ops", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

JOB = REPO.parent
STAGES = ("harvest", "select", "graph", "cells", "layout", "shards", "serving", "nameindex",
          "analyze")
LIB = "TauCeti"
DECL_PREFIX = f"decl:{LIB}:"


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.scope: str = args.scope
        self.out: Path = args.out.resolve()
        self.tauceti: Path = args.tauceti.resolve()
        self.inputs: Path = args.inputs.resolve()
        self.plans: Path = args.plans.resolve()
        self.dir = self.out / self.scope
        self.graph = self.dir / "graph"
        self.site = self.dir / "site"
        self.cells_dir = self.site / "cells"
        self.harvest = self.out / "harvest" / "tauceti.jsonl"
        self.layer = self.dir / "tauceti.jsonl"
        self.serving = self.dir / "serving.sqlite3"
        self.sidecar = self.dir / "serving-tables.json"

    def record(self, name: str, value) -> None:
        """Merge one stage's facts into DIR/<scope>/facts.json."""
        path = self.dir / "facts.json"
        facts = json.loads(path.read_text()) if path.exists() else {}
        facts[name] = value
        path.write_text(json.dumps(facts, indent=1, sort_keys=True) + "\n")


# ---- reading and writing JSONL ---------------------------------------------------------------

def read_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """(_meta, rows) of a JSONL file whose first line may be {"_meta": ...}."""
    meta: dict = {}
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if "_meta" in row and len(row) == 1:
                meta = row["_meta"]
            else:
                rows.append(row)
    return meta, rows


def write_jsonl(path: Path, meta: dict, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False, separators=(",", ":")) + "\n")
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


# ---- harvest ------------------------------------------------------------------------------------

def stage_harvest(ctx: Ctx) -> None:
    if ctx.harvest.exists():
        print(f"harvest: reusing {ctx.harvest}", file=sys.stderr)
        return
    import git_snapshot
    import lean_repo
    lean_repo.DECL_CAP = 10**9          # lifted here only; brain/ingest/lean_repo.py is unchanged
    snapshot = git_snapshot.read_text_snapshot(ctx.tauceti, scope=LIB, suffixes=(".lean",))
    rows, n_files = lean_repo.harvest_rows(snapshot, LIB)
    spec = lean_repo.SPECS["tauceti"]
    write_jsonl(ctx.harvest, {"source": f"{spec['owner']}/{spec['repo']}",
                              "repo": f"{spec['owner']}/{spec['repo']}", "lib": LIB,
                              "license": spec["license"], "commit": snapshot.commit,
                              "n_files": n_files, "n_decls": len(rows)}, rows)
    facts = {"commit": snapshot.commit, "n_files": n_files, "rows": len(rows),
             "unique": len({r["decl"] for r in rows}), "bytes": ctx.harvest.stat().st_size}
    (ctx.out / "harvest" / "facts.json").write_text(json.dumps(facts, indent=1) + "\n")
    print(f"harvest: {facts}", file=sys.stderr)


# ---- select -------------------------------------------------------------------------------------

def trough_names(trough: Path) -> dict[str, set[str]]:
    """Every declaration trough maps, by the file that names it: the keys of deps.json's
    `declarations` and its `missing` list, the keys of selection.json's `chosen` and
    `assuming`, and roadmaps.json's status `names`. The names that only appear inside a
    mapped declaration's statement or proof dependency lists are not mapped declarations."""
    deps = json.loads((trough / "deps.json").read_text())
    selection = json.loads((trough / "selection.json").read_text())
    roadmaps = json.loads((trough / "roadmaps.json").read_text())
    dep_names = set(deps.get("declarations") or {}) | set(deps.get("missing") or [])
    sel_names = set(selection.get("chosen") or {}) | set(selection.get("assuming") or {})
    road_names = {n for rm in roadmaps.get("roadmaps") or [] for n in rm.get("names") or []}
    return {"deps.json": dep_names, "selection.json": sel_names, "roadmaps.json": road_names}


def plan_names(decl_refs: Path) -> set[str]:
    """The roadmap-referenced TauCeti names: resolved_name if present, else name."""
    names = set()
    with decl_refs.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("resolution") == "tauceti":
                names.add(row.get("resolved_name") or row["name"])
    return names


def bmp_names(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8"))["declarations"]


def current_brain_names(nodes_path: Path) -> set[str]:
    out = set()
    with nodes_path.open(encoding="utf-8") as fh:
        for line in fh:
            if DECL_PREFIX in line:
                node = json.loads(line)
                if node.get("id", "").startswith(DECL_PREFIX):
                    out.add(node["label"])
    return out


def hybrid_union(sources: dict[str, set[str]], harvested: set[str]) -> dict:
    """The hybrid set: the union of `sources`, kept to names the harvest has a row for (only a
    harvested row carries the module and code a node needs). Counts per source too."""
    union = set().union(*sources.values())
    kept = union & harvested
    return {"union": union, "kept": kept,
            "counts": {"union": len(union), "kept": len(kept),
                       "not_harvested": len(union - harvested),
                       **{f"source:{k}": len(v) for k, v in sources.items()},
                       **{f"kept:{k}": len(v & harvested) for k, v in sources.items()}}}


def stage_select(ctx: Ctx) -> None:
    meta, rows = read_jsonl(ctx.harvest)
    if ctx.scope == "A":
        kept_rows = rows
        facts = {"layer_rows": len(rows), "layer_unique": len({r["decl"] for r in rows})}
    else:
        tc_bmp = set(bmp_names(ctx.inputs / "decls" / "tauceti.bmp"))
        current = current_brain_names(BRAIN / "data" / "nodes.jsonl")
        sources = trough_names(ctx.inputs / "trough" / "data" / "tauceti")
        sources["decl_refs.jsonl"] = plan_names(ctx.plans / "decl_refs.jsonl")
        sources["current_brain∩tauceti.bmp"] = current & tc_bmp
        harvested = {r["decl"] for r in rows}
        result = hybrid_union(sources, harvested)
        kept_rows = [r for r in rows if r["decl"] in result["kept"]]
        facts = {**result["counts"], "current_brain": len(current),
                 "union_in_tauceti_bmp": len(result["union"] & tc_bmp),
                 "union_not_harvested_but_in_bmp": len((result["union"] - harvested) & tc_bmp),
                 "harvest_in_bmp": len(harvested & tc_bmp), "layer_rows": len(kept_rows),
                 "layer_unique": len({r["decl"] for r in kept_rows})}
        (ctx.dir / "not_harvested.txt").write_text(
            "\n".join(sorted(result["union"] - harvested)) + "\n", encoding="utf-8")
    write_jsonl(ctx.layer, {**meta, "n_decls": len(kept_rows)}, kept_rows)
    ctx.record("select", facts)
    print(f"select {ctx.scope}: {facts}", file=sys.stderr)


# ---- graph --------------------------------------------------------------------------------------

def is_tauceti_node(node: dict) -> bool:
    return node["id"].startswith(DECL_PREFIX) or (
        node.get("type") == "container" and node.get("library") == LIB)


def strip_layer(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """The graph without its TauCeti layer: TauCeti decls and containers, and every edge the
    tauceti source minted or that touches one of them."""
    gone = {n["id"] for n in nodes if is_tauceti_node(n)}
    kept_nodes = [n for n in nodes if n["id"] not in gone]
    kept_edges = [e for e in edges
                  if (e.get("provenance") or {}).get("source") != "tauceti"
                  and e["src"] not in gone and e["dst"] not in gone]
    return kept_nodes, kept_edges, {"nodes_removed": len(gone),
                                     "edges_removed": len(edges) - len(kept_edges)}


def mathlib_universe(nodes: list[dict]) -> dict[str, str]:
    """Bare FQ name -> decl:Mathlib:* id, as build_common builds `mathlib_fq_ids`, minus its
    decl_renames and decl-oracle filters (they need the Mathlib checkout): dotless names that
    are also the last segment of a dotted Mathlib name are left out."""
    decls = {n["label"]: n["id"] for n in nodes
             if n["id"].startswith("decl:Mathlib:") and n.get("label")}
    trailing = {d.rsplit(".", 1)[1] for d in decls if "." in d}
    return {d: i for d, i in decls.items() if "." in d or d not in trailing}


def stage_graph(ctx: Ctx) -> None:
    import build_common
    import build_shards
    t0 = time.monotonic()
    nmeta, nodes = read_jsonl(BRAIN / "data" / "nodes.jsonl")
    emeta, edges = read_jsonl(BRAIN / "data" / "edges.jsonl")
    old_tc = sum(1 for n in nodes if n["id"].startswith(DECL_PREFIX))
    old_inv = sum(1 for e in edges if e["kind"] == "invocation"
                  and (e.get("provenance") or {}).get("source") == "tauceti")
    nodes, edges, stripped = strip_layer(nodes, edges)
    containers = {n["id"]: n for n in nodes if n.get("type") == "container"}
    concepts = {n["id"] for n in nodes if n.get("type") == "concept"}
    slug_lookup = {n["slug"]: n["id"] for n in nodes if n.get("type") == "concept" and n.get("slug")}
    universe = mathlib_universe(nodes)
    tc_meta, tc_rows = build_common._read_frontier_jsonl(ctx.layer)
    before = set(containers)
    decl_nodes: list[dict] = []
    new_edges: list[dict] = []
    stats = build_common._frontier_stats()
    build_common._frontier_repo_layer(
        lib=LIB, rows=tc_rows, n_files=tc_meta.get("n_files"),
        pin=(tc_meta.get("commit") or "")[:12], source="tauceti",
        tree_method="file-tree (tauceti.jsonl)", ref_method="tauceti reference URL",
        containers=containers, decl_nodes=decl_nodes, edges=new_edges,
        slug_lookup=slug_lookup, ensure_concept=lambda qid: qid in concepts, stats=stats,
        agent_links=None, mathlib_names=universe)
    new_containers = [containers[c] for c in sorted(set(containers) - before)]
    minted = new_containers + decl_nodes
    build_common.apply_facets(minted, new_edges, [])
    build_common.aggregate_facets(minted, new_edges)
    nodes += minted
    edges += new_edges
    write_jsonl(ctx.graph / "nodes.jsonl", nmeta, nodes)
    write_jsonl(ctx.graph / "edges.jsonl", emeta, edges)
    xref_blob, *_ = build_shards._render_outputs(
        edges_path=ctx.graph / "edges.jsonl", community_edges_path=None,
        source_registry_path=REPO / "catalog" / "data" / "source_registry.json")
    ctx.site.mkdir(parents=True, exist_ok=True)
    (ctx.site / "xref_index.json").write_text(xref_blob, encoding="utf-8")
    kinds = Counter(e["kind"] for e in new_edges)
    facts = {**stripped, "old_tauceti_decls": old_tc, "old_tauceti_invocation_edges": old_inv,
             "nodes": len(nodes), "edges": len(edges), "layer_stats": stats,
             "layer_edges_by_kind": dict(kinds), "mathlib_universe": len(universe),
             "nodes_bytes": (ctx.graph / "nodes.jsonl").stat().st_size,
             "edges_bytes": (ctx.graph / "edges.jsonl").stat().st_size,
             "seconds": round(time.monotonic() - t0, 1)}
    ctx.record("graph", facts)
    print(f"graph {ctx.scope}: {facts}", file=sys.stderr)


# ---- cells, layout, shards, serving --------------------------------------------------------------

def stage_cells(ctx: Ctx) -> None:
    import build_cells
    state = REPO / "bot" / "state"
    paths = build_cells.CellInputPaths(
        nodes=ctx.graph / "nodes.jsonl", edges=ctx.graph / "edges.jsonl",
        edges_links=ctx.graph / "edges_links.jsonl",          # not in Git: absent
        grounding_overrides=REPO / "catalog" / "data" / "grounding_overrides.jsonl",
        discovery_rejected=BRAIN / "data" / "discovery_rejected.jsonl",
        bot_cut_log=state / "cut_log.json", bot_seed_queue=state / "seed_queue.json",
        bot_recycle_queue=state / "recycle_queue.json", bot_brain_queue=state / "brain_queue.json",
        bot_pool_candidates=state / "pool_candidates.json")
    cells, synapses, meta, review = build_cells.build(do_layout=False, input_paths=paths)
    build_cells.write_jsonl(ctx.graph / "cells.jsonl", meta, cells)
    build_cells.write_jsonl(ctx.graph / "synapses.jsonl", build_cells._synapse_meta(meta), synapses)
    ctx.record("cells", {**meta["counts"],
                         "cells_bytes": (ctx.graph / "cells.jsonl").stat().st_size,
                         "synapses_bytes": (ctx.graph / "synapses.jsonl").stat().st_size})


def stage_layout(ctx: Ctx) -> None:
    import build_cells
    import layout
    meta, rows = read_jsonl(ctx.graph / "cells.jsonl")
    _, synapses = read_jsonl(ctx.graph / "synapses.jsonl")
    cells = {c["id"]: c for c in rows}
    bubbled = sum(1 for c in cells if layout._bubble_library(c) is not None
                  and len(cells[c].get("organs") or ()) <= 1)
    t0 = time.monotonic()
    layout.layout_cells(cells, synapses)
    seconds = time.monotonic() - t0
    build_cells.write_jsonl(ctx.graph / "cells.jsonl", meta, [cells[k] for k in sorted(cells)])
    radius = max((x * x + y * y) ** 0.5 for x, y in (c["xy"] for c in cells.values()))
    ctx.record("layout", {"cells": len(cells), "bubbled": bubbled, "core": len(cells) - bubbled,
                          "seconds": round(seconds, 1), "map_radius": round(radius)})


def stage_shards(ctx: Ctx) -> None:
    import shutil
    import build_cell_shards
    if ctx.cells_dir.exists():
        shutil.rmtree(ctx.cells_dir)
    build_cell_shards.build_cell_shards(
        cells_path=ctx.graph / "cells.jsonl", synapses_path=ctx.graph / "synapses.jsonl",
        nodes_path=ctx.graph / "nodes.jsonl", edges_path=ctx.graph / "edges.jsonl",
        frontier_path=BRAIN / "data" / "frontier.jsonl",
        frontier_graph_path=BRAIN / "data" / "frontier_graph.json",
        output_dir=ctx.cells_dir, scratch_parent=ctx.site)
    files = [p for p in ctx.cells_dir.rglob("*") if p.is_file()]
    whole = {p.name: p.stat().st_size for p in files if p.parent == ctx.cells_dir
             and p.name in ("aliases.json", "labels.json", "supercells.json", "explorer.json",
                            "manifest.json")}
    ctx.record("shards", {"files": len(files), "bytes": sum(p.stat().st_size for p in files),
                          "whole_files": whole})


def stage_serving(ctx: Ctx) -> None:
    import build_serving_db
    sidecar = build_serving_db.build(ctx.cells_dir, ctx.graph / "nodes.jsonl", ctx.serving,
                                     xref_path=ctx.site / "xref_index.json",
                                     sidecar_path=ctx.sidecar)
    ctx.record("serving", {"bytes": ctx.serving.stat().st_size,
                           "rows": {t: v["rows"] for t, v in sidecar["tables"].items()}})


def stage_nameindex(ctx: Ctx) -> None:
    if ctx.scope != "B":
        print("nameindex: scope B only", file=sys.stderr)
        return
    import names
    ctx.record("nameindex", names.add_name_index(
        ctx.serving, {"Mathlib": ctx.inputs / "decls" / "mathlib.bmp",
                      "TauCeti": ctx.inputs / "decls" / "tauceti.bmp"}))


def stage_analyze(ctx: Ctx) -> None:
    import analyze
    analyze.run(ctx)


# ---- the runner ---------------------------------------------------------------------------------

def run_stages(args: argparse.Namespace, stages: list[str]) -> int:
    ctx = Ctx(args)
    ctx.dir.mkdir(parents=True, exist_ok=True)
    timings_path = ctx.dir / "timings.json"
    timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}
    base = [sys.executable, str(Path(__file__).resolve())]
    opts = ["--scope", ctx.scope, "--out", str(ctx.out), "--tauceti", str(ctx.tauceti),
            "--inputs", str(ctx.inputs), "--plans", str(ctx.plans)]
    for name in stages:
        log = (ctx.dir / f"{name}.log").open("w")
        t0 = time.monotonic()
        proc = subprocess.Popen([*base, "stage", name, *opts], stdout=log, stderr=subprocess.STDOUT)
        _, status, usage = os.wait4(proc.pid, 0)
        wall = time.monotonic() - t0
        rc = os.waitstatus_to_exitcode(status)
        proc.returncode = rc
        log.close()
        timings[name] = {"wall_s": round(wall, 1), "max_rss_mb": round(usage.ru_maxrss / 1024),
                         "user_s": round(usage.ru_utime, 1), "rc": rc}
        timings_path.write_text(json.dumps(timings, indent=1) + "\n")
        print(f"{ctx.scope} {name}: {timings[name]}", flush=True)
        if rc != 0:
            print(f"{name} failed; see {ctx.dir / (name + '.log')}", file=sys.stderr)
            return rc
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("run", "stage"))
    ap.add_argument("name", nargs="?")
    ap.add_argument("--scope", choices=("A", "B"), required=True)
    ap.add_argument("--out", type=Path, default=JOB / "out")
    ap.add_argument("--tauceti", type=Path, default=JOB / "tauceti")
    ap.add_argument("--inputs", type=Path, default=JOB / "inputs")
    ap.add_argument("--plans", type=Path, default=JOB / "plans-export")
    ap.add_argument("--stages", default=",".join(STAGES))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.command == "run":
        stages = [s for s in args.stages.split(",") if s]
        unknown = set(stages) - set(STAGES)
        if unknown:
            raise SystemExit(f"unknown stages {sorted(unknown)}")
        return run_stages(args, stages)
    if args.name not in STAGES:
        raise SystemExit(f"unknown stage {args.name!r}")
    globals()[f"stage_{args.name}"](Ctx(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
