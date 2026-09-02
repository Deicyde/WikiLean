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
those artifacts are emitted any more. Historical files are outside this
builder's ownership and must be removed by an explicit migration, not as a
side effect of rebuilding these two assets.

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

Publication owns only those two files. It never swaps or prunes their parent
directory, so the independently built cells/ tree and unrelated assets remain
untouched.

Run: python3 brain/build_shards.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path

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


def _publish_outputs(xref_blob: str, sources_blob: str) -> None:
    """Publish this stage's two files via atomic replaces, with pair rollback."""
    OUT_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".brain-shards.", dir=OUT_DIR.parent))
    try:
        staged = (
            (staging / "xref_index.json", OUT_DIR / "xref_index.json", xref_blob),
            (staging / "sources.json", OUT_DIR / "sources.json", sources_blob),
        )
        for source, _destination, blob in staged:
            source.write_text(blob, encoding="utf-8")

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        backups: list[tuple[Path, Path | None]] = []
        for index, (_source, destination, _blob) in enumerate(staged):
            backup = staging / f"previous-{index}"
            if destination.exists():
                try:
                    os.link(destination, backup)
                except OSError:
                    shutil.copy2(destination, backup)
                backups.append((destination, backup))
            else:
                backups.append((destination, None))

        try:
            for source, destination, _blob in staged:
                os.replace(source, destination)
        except BaseException:
            for destination, backup in reversed(backups):
                if backup is None:
                    destination.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, destination)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    t0 = time.monotonic()
    edges_path = BRAIN_DATA / "edges.jsonl"
    if not edges_path.exists():
        raise SystemExit(f"missing {edges_path} — run python3 brain/build_snapshot.py")

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

    # ---- owned two-file publication -----------------------------------------
    _publish_outputs(
        json.dumps(
            {p: ns for p, ns in xref_index.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "layers": reg["layers"],
                "our_data_license": reg["our_data_license"],
                "sources": src_out,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )

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
