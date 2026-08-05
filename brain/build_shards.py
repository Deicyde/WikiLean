#!/usr/bin/env python3
"""Build the two still-live top-level brain assets: xref_index.json + sources.json.

Historically this script built the whole v2 particle layer — per-node
neighborhood shards (q*/decl_*/xref_*/path_*/lit_*.json), manifest.json,
labels.json, aliases.json and views/xref_explorer.json, ~340 MB. BRAIN v3
(docs/BRAIN-V3.md) made the CELL the node: the /brain page reads
/assets/brain/cells/* (brain/build_cell_shards.py) and the community-edit
endpoint oracle validates against cells/aliases.json ∪ cells/supercells.json
(wiki/src/brain-api.ts atomIdForOrgan), so the per-node layer is retired
(the GET /api/brain/node route is deleted outright — plain 404) and none of
those artifacts are emitted any more. The atomic swap below also prunes any stale copies of them
out of site/assets/brain/ on the first run.

Two outputs from this builder are still LIVE and remain:

  xref_index.json  external-page -> [node ids] reverse index. Powers the
                   community-edge cross-pollination in GET /api/brain/edges
                   (wiki/src/brain-edits.ts getXrefIndex; the Worker also
                   inverts it per isolate into node -> pages, which replaced
                   the v2 shard entries' kind=="xref" edge lists).
  sources.json     the transparency legend (/brain Sources view): the
                   flattened provenance registry, one entry per source with
                   layer + license. catalog/data/source_registry.json is the
                   single source of truth.

Inputs: brain/data/edges.jsonl (kind=="xref" rows only; the split-out
edges_links.jsonl holds only kind=="links" rows by construction and is not
read) and brain/data/community_edges.jsonl (graduated community xrefs,
optional). Both files' xref rows fold into the index.

The swap stays atomic and still CARRIES the nested cells/ tree (the v3 atom
layer) across — build_cell_shards.py writes it into the same directory, and a
plain swap would delete it.

Run: python3 brain/build_shards.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from collections import defaultdict

from build_common import BRAIN_DATA, ROOT

OUT_DIR = ROOT / "site" / "assets" / "brain"

# ---- the shared prefix-sharding scheme -------------------------------------
# build_cell_shards.py imports these + shard_key: the v3 cell shards reuse this
# scheme verbatim (it mirrors wiki/scripts/build-decl-index.ts), so the
# constants live here even though THIS builder no longer shards anything.
MAX_SHARD_BYTES = 150_000
MIN_KEY_LEN = 2
MAX_KEY_LEN = 64            # termination guard for pathological collisions
PAD = "_"


def shard_key(node_id: str, length: int) -> str:
    """Mirror build-decl-index.ts: lowercase [a-z0-9], anything else PAD."""
    k = ""
    for i in range(length):
        if i < len(node_id):
            c = node_id[i].lower()
            k += c if ("a" <= c <= "z" or "0" <= c <= "9") else PAD
        else:
            k += PAD
    return k

# Subdirectories of OUT_DIR owned by OTHER builders, carried across the swap.
# cells/ is the v3 atom layer (brain/build_cell_shards.py): the /brain page and
# the whole agent API read it, so deleting it here would take the site down
# until the next cell build.
NESTED = ("cells",)


def main() -> int:
    t0 = time.monotonic()
    edges_path = BRAIN_DATA / "edges.jsonl"
    if not edges_path.exists():
        raise SystemExit(f"missing {edges_path} — run the earlier brain/build_*.py steps")

    # ---- xref_index.json: external-page -> [node ids that xref it] ----------
    xref_index: dict[str, list[str]] = defaultdict(list)
    n_edges = 0
    with edges_path.open() as fh:
        meta = json.loads(next(fh))["_meta"]
        for line in fh:
            e = json.loads(line)
            n_edges += 1
            if e["kind"] == "xref":
                xref_index[e["dst"]].append(e["src"])

    # Graduated community edges (docs/BRAIN-EDITS-ROADMAP.md phase 4):
    # harvest_community_edges.py snapshots the live D1 tail here. Fold their
    # xref targets into the reverse index so cross-pollination sees graduated
    # links from the static base too (the live overlay still queries D1 for
    # the tail).
    comm = ROOT / "brain" / "data" / "community_edges.jsonl"
    n_community = 0
    if comm.exists():
        for line in comm.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            n_community += 1
            if e.get("kind") == "xref":
                xref_index[e["dst"]].append(e["src"])

    # ---- sources.json: the transparency legend (/brain Sources view) --------
    # The flattened provenance registry, one entry per external database with
    # layer + license; catalog/data/source_registry.json is the SSOT.
    reg = json.loads((ROOT / "catalog" / "data" / "source_registry.json").read_text())
    src_out: list[dict] = []

    def _add(key: str, e: dict, group: str) -> None:
        src_out.append({k: e.get(k, "") for k in
                        ("name", "homepage", "layer", "kind", "our_provenance",
                         "target_license", "wikidata_property", "note")}
                       | {"key": key, "group": group})

    _add(reg["spine"]["key"], reg["spine"], "spine")
    for grp in ("node_sources", "edge_sources", "crossref_sources",
                "literature_sources", "frontier_sources", "brain_sources"):
        for k, e in reg.get(grp, {}).items():
            _add(k, e, grp)

    # ---- atomic directory swap (carrying the nested v3 tree) ----------------
    # Renaming OUT_DIR aside and rmtree()ing it is also what PRUNES any stale
    # v2 per-node files still on disk: only the two files above + the carried
    # NESTED trees survive a run.
    tmp = OUT_DIR.parent / ".brain.tmp"
    old = OUT_DIR.parent / ".brain.old"
    for stale in (tmp, old):
        if stale.exists():
            shutil.rmtree(stale)
    tmp.mkdir(parents=True)
    (tmp / "xref_index.json").write_text(
        json.dumps({p: ns for p, ns in xref_index.items()},
                   ensure_ascii=False, separators=(",", ":")))
    (tmp / "sources.json").write_text(json.dumps(
        {"layers": reg["layers"], "our_data_license": reg["our_data_license"],
         "sources": src_out}, ensure_ascii=False, separators=(",", ":")))
    if OUT_DIR.exists():
        OUT_DIR.rename(old)
        for name in NESTED:
            keep = old / name
            if keep.exists():
                keep.rename(tmp / name)
                print(f"  carried {name}/ across the swap (v3 atom layer)",
                      file=sys.stderr)
    tmp.rename(OUT_DIR)
    if old.exists():
        shutil.rmtree(old)

    print(f"xref_index.json: {len(xref_index)} pages, "
          f"{sum(len(v) for v in xref_index.values())} node links "
          f"({n_edges} ontology edges scanned + {n_community} community rows; "
          f"edges.jsonl generated_at {meta.get('generated_at', '?')})")
    print(f"sources.json: {len(src_out)} sources")
    print("  (v2 per-node shards retired — docs/BRAIN-V3.md phase 5; "
          "cells/ is built by build_cell_shards.py)")
    print(f"  wall: {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
