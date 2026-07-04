#!/usr/bin/env python3
"""Roll TheoremGraph's decl→decl `depends` edges up to the BRAIN's coarse grains.

One streaming pass over catalog/.cache/formal_dependency.csv (11.3M rows), joined
with statement_formal.csv's id → (decl_name, file, module) map, aggregated to three
grains that line up with the containment tree (brain/SCHEMA.md):

  file    path:<file_path>                      (file_path is already library-rooted)
  dir     path:<deepest hierarchy.json prefix>  (catalog/data/hierarchy.json node paths)
  module  path:<Library>/<top module>

Per aggregated edge: w_types = {sig, def, proof} counting DISTINCT (src_decl, dep_decl)
pairs per bucket (sig bucket = sig+field+extends; docref excluded entirely), plus up to
3 top witnessing decl-name pairs by raw row frequency. Self-loops excluded per grain.

Outputs (atomic writes; _meta as the first JSONL line):
  brain/data/rollup_edges.{file,dir,module}.jsonl
  brain/data/hub_stats.json   — per grain, top-50 destinations by inbound sig-weight

Run: python3 brain/build_rollups.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEP = REPO / "catalog" / ".cache" / "formal_dependency.csv"
STMT = REPO / "catalog" / ".cache" / "statement_formal.csv"
HIER = REPO / "catalog" / "data" / "hierarchy.json"
OUTDIR = HERE / "data"

GRAINS = ("file", "dir", "module")
BUCKETS = ("sig", "def", "proof")
# kernel edge_type → w_types bucket bit; docref is absent on purpose (excluded entirely)
BUCKET_BIT = {"sig": 1, "field": 1, "extends": 1, "def": 2, "proof": 4}
SHIFT = 19          # decl indices fit in 19 bits (388,105 < 2^19); pair key = src<<19|dep
TOP_WITNESSES = 3
TOP_HUBS = 50
csv.field_size_limit(10 ** 9)


def sha16(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()[:16]


def load_decls() -> tuple[dict[str, int], list[str], tuple[list[str], list[str], list[str]]]:
    """statement_formal.csv → (id→idx, decl names, per-grain node id per decl)."""
    hier = json.loads(HIER.read_text())
    libraries = hier["libraries"]

    def grain_nodes_for(module: str) -> tuple[str, str]:
        parts = module.split(".")
        lib = parts[0]
        L = libraries.get(lib)
        if L is None:
            print(f"WARNING: library {lib!r} missing from hierarchy.json — "
                  f"dir grain falls back to the library root", file=sys.stderr)
        comps = [lib]
        node = L["modules"] if L else {}
        for c in parts[1:]:
            child = node.get(c)
            if child is None:
                break
            comps.append(c)
            node = child.get("sub") or {}
        dir_node = "path:" + "/".join(comps)
        mod_node = "path:" + (f"{lib}/{parts[1]}" if len(parts) > 1 else lib)
        return dir_node, mod_node

    id2idx: dict[str, int] = {}
    names: list[str] = []
    file_nodes: list[str] = []
    dir_nodes: list[str] = []
    mod_nodes: list[str] = []
    file_memo: dict[str, str] = {}
    mod_memo: dict[str, tuple[str, str]] = {}
    with STMT.open(newline="") as fh:
        for r in csv.DictReader(fh):
            sid = r["statement_id"]
            if sid in id2idx:
                continue
            id2idx[sid] = len(names)
            names.append(r["decl_name"])
            fp = r["file_path"]
            fnode = file_memo.get(fp)
            if fnode is None:
                fnode = file_memo[fp] = "path:" + fp
            file_nodes.append(fnode)
            mod = r["module"]
            dm = mod_memo.get(mod)
            if dm is None:
                dm = mod_memo[mod] = grain_nodes_for(mod)
            dir_nodes.append(dm[0])
            mod_nodes.append(dm[1])
    return id2idx, names, (file_nodes, dir_nodes, mod_nodes)


def stream_pairs(id2idx: dict[str, int]) -> tuple[dict[int, int], dict[str, int]]:
    """formal_dependency.csv → {src<<SHIFT|dep: rowcount<<3 | bucket mask} + counters."""
    pairs: dict[int, int] = {}
    stats = {"rows": 0, "docref_skipped": 0, "unknown_edge_type": 0, "unknown_id": 0}
    get_idx = id2idx.get
    get_bit = BUCKET_BIT.get
    with DEP.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header[:3] != ["src_id", "dep_id", "edge_type"]:
            raise SystemExit(f"unexpected formal_dependency.csv header: {header[:3]}")
        for row in reader:
            stats["rows"] += 1
            bit = get_bit(row[2])
            if bit is None:
                stats["docref_skipped" if row[2] == "docref"
                      else "unknown_edge_type"] += 1
                continue
            si = get_idx(row[0])
            di = get_idx(row[1])
            if si is None or di is None:
                stats["unknown_id"] += 1
                continue
            key = si << SHIFT | di
            packed = pairs.get(key)
            pairs[key] = 8 | bit if packed is None else (packed + 8) | bit
    return pairs, stats


def aggregate(pairs: dict[int, int],
              grain_nodes: tuple[list[str], list[str], list[str]]
              ) -> list[dict[tuple[str, str], list]]:
    """pairs → per grain {(src_node, dst_node): [sig, def, proof, top-3 witnesses]}.
    Witness entries are (-rowcount, src_idx, dep_idx), kept sorted ascending so
    [-1] is the weakest; ties break on CSV order for determinism."""
    mask_lo = (1 << SHIFT) - 1
    edges: list[dict[tuple[str, str], list]] = [{}, {}, {}]
    for key, packed in pairs.items():
        si = key >> SHIFT
        di = key & mask_lo
        wit = (-(packed >> 3), si, di)
        mask = packed & 7
        for g in range(3):
            nodes = grain_nodes[g]
            s = nodes[si]
            d = nodes[di]
            if s == d:
                continue
            rec = edges[g].get((s, d))
            if rec is None:
                rec = edges[g][(s, d)] = [0, 0, 0, []]
            if mask & 1:
                rec[0] += 1
            if mask & 2:
                rec[1] += 1
            if mask & 4:
                rec[2] += 1
            top = rec[3]
            if len(top) < TOP_WITNESSES:
                top.append(wit)
                if len(top) == TOP_WITNESSES:
                    top.sort()
            elif wit < top[-1]:
                top[-1] = wit
                top.sort()
    return edges


def main() -> int:
    t0 = time.monotonic()
    for p in (DEP, STMT, HIER):
        if not p.exists():
            raise SystemExit(f"missing {p}")

    id2idx, names, grain_nodes = load_decls()
    t_decls = time.monotonic()
    pairs, stats = stream_pairs(id2idx)
    t_pairs = time.monotonic()
    edges = aggregate(pairs, grain_nodes)
    t_agg = time.monotonic()

    hier_meta = json.loads(HIER.read_text())["meta"]
    dep_st = DEP.stat()
    base_meta = {
        "source": "uw-math-ai/math-graph formal_dependency.csv x statement_formal.csv "
                  "(TheoremGraph)",
        "provenance_source": "theoremgraph",   # key in catalog/data/source_registry.json
        # snapshot mtime, NOT build time — rebuilds of the same CSVs are byte-identical
        "generated_at": datetime.fromtimestamp(dep_st.st_mtime, tz=timezone.utc)
                        .isoformat(timespec="seconds"),
        "pins": {
            "formal_dependency": {"bytes": dep_st.st_size, "sha256": sha16(DEP)},
            "statement_formal": {"bytes": STMT.stat().st_size, "sha256": sha16(STMT)},
            "hierarchy": {"generated_at": hier_meta["generated_at"],
                          "source_sha256": hier_meta["source_sha256"]},
        },
        "kind": "depends",
        "license": "CC-BY-SA-4.0 (TheoremGraph-derived edge facts; attribution: "
                   "uw-math-ai/math-graph)",
        "note": "w_types count DISTINCT (src_decl,dep_decl) pairs per bucket "
                "(sig bucket = sig+field+extends kernel edge types; def; proof); "
                "docref rows excluded entirely; self-loops at this grain excluded; "
                "top_witnesses = up to 3 [src_decl, dep_decl] FQ decl-name pairs by "
                "raw row frequency over the included buckets. Default render layer "
                "= sig.",
    }

    OUTDIR.mkdir(parents=True, exist_ok=True)
    hubs: dict[str, list[dict]] = {}
    for g, grain in enumerate(GRAINS):
        rows = sorted(edges[g].items(),
                      key=lambda kv: (-kv[1][0], -(kv[1][1] + kv[1][2]), kv[0]))
        inbound: dict[str, list[int]] = {}
        out = OUTDIR / f"rollup_edges.{grain}.jsonl"
        tmp = out.with_suffix(".jsonl.tmp")
        with tmp.open("w") as fh:
            fh.write(json.dumps({"_meta": {**base_meta, "grain": grain,
                                           "n_edges": len(rows)}},
                                ensure_ascii=False) + "\n")
            for (s, d), rec in rows:
                fh.write(json.dumps({
                    "src": s, "dst": d,
                    "w_types": {"sig": rec[0], "def": rec[1], "proof": rec[2]},
                    "top_witnesses": [[names[si], names[di]] for _, si, di in rec[3]],
                }, ensure_ascii=False) + "\n")
                acc = inbound.setdefault(d, [0, 0])
                acc[0] += rec[0]
                acc[1] += 1
        tmp.replace(out)
        hubs[grain] = [
            {"node": n, "in_sig": v[0], "in_edges": v[1]}
            for n, v in sorted(inbound.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ][:TOP_HUBS]

    hub_out = OUTDIR / "hub_stats.json"
    tmp = hub_out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "_meta": {**base_meta,
                  "note": f"Per grain, the top-{TOP_HUBS} destination nodes by inbound "
                          "sig-weight (sum of w_types.sig over inbound rolled-up edges, "
                          "self-loops excluded) — the hub-suppression input for "
                          "renderers. in_edges = distinct inbound source nodes."},
        "grains": hubs,
    }, ensure_ascii=False, indent=1))
    tmp.replace(hub_out)

    sig_file_edges = sum(1 for rec in edges[0].values() if rec[0] > 0)
    print(f"rollups: {stats['rows']} dep rows -> {len(pairs)} distinct decl pairs "
          f"(docref {stats['docref_skipped']}, unknown edge_type "
          f"{stats['unknown_edge_type']}, unknown id {stats['unknown_id']} skipped)")
    for g, grain in enumerate(GRAINS):
        print(f"  {grain:6} {len(edges[g]):>7} edges "
              f"({(OUTDIR / f'rollup_edges.{grain}.jsonl').stat().st_size / 2**20:.1f} MB)")
    print(f"  file-grain edges with sig>0: {sig_file_edges}")
    print(f"  wall: decls {t_decls - t0:.1f}s + deps {t_pairs - t_decls:.1f}s + "
          f"aggregate {t_agg - t_pairs:.1f}s + write {time.monotonic() - t_agg:.1f}s "
          f"= {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
