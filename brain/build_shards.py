#!/usr/bin/env python3
"""Build per-node neighborhood shards — SCHEMA.md's locality law as static JSON.

Reads brain/data/{nodes.jsonl, edges.jsonl, rollup_edges.{module,dir}.jsonl} and
writes site/assets/brain/: prefix-named shard files, each a JSON object mapping
node id → neighborhood entry, plus manifest.json. The scheme is
wiki/scripts/build-decl-index.ts's longest-prefix sharding (normalize, start at
2-char keys, recursively split oversize shards, prefix-free leaves): a client
loads manifest.json once, then ANY node is one fetch away — the shard named by
the longest manifest key that prefixes the node's padded normalized id.

Entry per node:
  node        the nodes.jsonl payload, verbatim
  breadcrumb  containment path root→node (containers + decls; concepts float)
  children    first CHILD_CAP direct children + total count (containers)
  edges       ontology edges both directions, capped at EDGE_CAP per direction
              (highest priority first), with counts + truncated flags
  rollup      containers only: `depends` rollups at module/dir grain, top
              ROLLUP_CAP[grain] per direction by sig weight. File grain is
              query-side only (brain/query.py): its path:<file>.lean endpoints
              are not container nodes, and dir grain already aggregates to the
              deepest hierarchy prefix — the same node — for leaf containers.

Shards are a rendering artifact: provenance dicts are factored into a
manifest-level `prov` table (edges carry an index) and the heavy evidence
lists (depends `witnesses`, rollup `top_witnesses`) are trimmed to their first
pair. The full edges live in brain/data/*.jsonl, served by brain/query.py.

NOTE: wiki/scripts/build-public.ts does not copy this directory yet — it copies
an explicit file list. Shipping needs a wipe-then-recursive-copy of
site/assets/brain/ → wiki/public/assets/brain/ there (the /brain UI task).

Run: python3 brain/build_shards.py
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from build_common import BRAIN_DATA, KIND_ORDER, ROOT

csv.field_size_limit(10 ** 9)

OUT_DIR = ROOT / "site" / "assets" / "brain"

MAX_SHARD_BYTES = 150_000
MIN_KEY_LEN = 2
MAX_KEY_LEN = 64            # termination guard for pathological collisions
PAD = "_"
EDGE_CAP = 200              # ontology edges kept per direction (SCHEMA shard cap)
# tree grain = each node's full aggregate depends-flow at its own depth (the
# canvas draws these between sibling bubbles at every zoom level)
ROLLUP_CAP = {"tree": 48}               # depends rollups kept per direction
# Complete child lists (largest file = 501 decls, largest dir ~200 files): the
# /brain bubble canvas packs every child, so truncation = invisible bubbles.
CHILD_CAP = 600
CONF_RANK = {"high": 0, "medium": 1, "low": 2}


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


def rollup_confidence(sig: int) -> str:
    # same thresholds as build_common's lifted-depends confidence
    return "high" if sig >= 5 else "medium" if sig >= 2 else "low"


def main() -> int:
    t0 = time.monotonic()
    metas: dict[str, dict] = {}
    input_files: dict[str, Path] = {}

    def open_meta(name: str):
        p = BRAIN_DATA / name
        if not p.exists():
            raise SystemExit(f"missing {p} — run the earlier brain/build_*.py steps")
        fh = p.open()
        metas[name] = json.loads(next(fh))["_meta"]
        input_files[name] = p
        return fh

    nodes: dict[str, dict] = {}
    with open_meta("nodes.jsonl") as fh:
        for line in fh:
            n = json.loads(line)
            nodes[n["id"]] = n

    prov_ix: dict[tuple, int] = {}
    prov_list: list[dict] = []

    def prov_index(p: dict) -> int:
        key = (p["source"], p["method"], p["pin"])
        i = prov_ix.get(key)
        if i is None:
            i = prov_ix[key] = len(prov_list)
            prov_list.append(p)
        return i

    # ---- edges.jsonl: contains → tree maps; ontology → per-node entries -----
    parent: dict[str, str] = {}
    children: dict[str, list[str]] = defaultdict(list)
    edges_out: dict[str, list] = defaultdict(list)
    edges_in: dict[str, list] = defaultdict(list)
    n_ontology = 0
    with open_meta("edges.jsonl") as fh:
        for line in fh:
            e = json.loads(line)
            if e["kind"] == "contains":
                parent[e["dst"]] = e["src"]
                children[e["src"]].append(e["dst"])
                continue
            n_ontology += 1
            kind = e["kind"]
            ev = e["evidence"]
            if kind == "depends" and len(ev.get("witnesses") or []) > 1:
                ev = {**ev, "witnesses": ev["witnesses"][:1]}
            weight = ev.get("weight", 0) if kind == "depends" else 0
            rank = (KIND_ORDER.index(kind), CONF_RANK.get(e["confidence"], 3), -weight)
            pi = prov_index(e["provenance"])
            base = {"kind": kind, "confidence": e["confidence"], "evidence": ev,
                    "prov": pi}
            edges_out[e["src"]].append((rank, e["dst"], {"id": e["dst"], **base}))
            if e["dst"] in nodes:
                edges_in[e["dst"]].append((rank, e["src"], {"id": e["src"], **base}))

    # ---- depends rollups at the shipped grains ------------------------------
    rollups: dict[str, dict[str, dict]] = defaultdict(dict)   # id → grain → block
    n_rollup_attached = 0
    for grain in ("tree",):
        name = f"rollup_edges.{grain}.jsonl"
        outs: dict[str, list] = defaultdict(list)
        ins: dict[str, list] = defaultdict(list)
        with open_meta(name) as fh:
            meta = metas[name]
            pi = prov_index({"source": meta["provenance_source"],
                             "method": name, "pin": meta["generated_at"][:10]})
            for line in fh:
                r = json.loads(line)
                sig = r["w_types"]["sig"]
                rec = (-sig, r["w_types"], r["top_witnesses"][:1], r.get("lift"))
                if r["src"] in nodes:
                    outs[r["src"]].append((*rec, r["dst"]))
                if r["dst"] in nodes:
                    ins[r["dst"]].append((*rec, r["src"]))
        cap = ROLLUP_CAP[grain]
        for nid in sorted(set(outs) | set(ins)):
            block = {}
            for direction, per in (("out", outs), ("in", ins)):
                rows = sorted(per.get(nid, []), key=lambda t: (t[0], t[4]))
                block[direction] = [
                    {"id": other, "kind": "depends", "confidence": rollup_confidence(-neg),
                     "evidence": {"w_types": wt, "top_witnesses": tw,
                                  **({"lift": lf} if lf is not None else {})},
                     "prov": pi}
                    for neg, wt, tw, lf, other in rows[:cap]]
            block["counts"] = {"out": len(outs.get(nid, [])), "in": len(ins.get(nid, []))}
            block["truncated"] = {d: block["counts"][d] > len(block[d]) for d in ("out", "in")}
            n_rollup_attached += len(block["out"]) + len(block["in"])
            rollups[nid][grain] = block

    # ---- assemble one entry per node ----------------------------------------
    def breadcrumb(nid: str) -> list[dict]:
        chain, seen = [nid], {nid}
        while chain[0] in parent and parent[chain[0]] not in seen:
            chain.insert(0, parent[chain[0]])
            seen.add(chain[0])
        return [{"id": i, "label": nodes[i].get("label"), "type": nodes[i]["type"]}
                for i in chain if i in nodes]

    # concepts anchored per container (inbound formalizes) — baked into child
    # summaries so the bubble canvas can badge children without prefetching
    # every child shard
    n_concepts: dict[str, int] = defaultdict(int)
    for nid, rows in edges_in.items():
        if nid in nodes and nodes[nid]["type"] == "container":
            n_concepts[nid] = sum(1 for _, _, item in rows if item["kind"] == "formalizes")

    def child_summary(nid: str) -> dict:
        conts, decls = [], []
        for cid in children.get(nid, []):
            (conts if nodes[cid]["type"] == "container" else decls).append(nodes[cid])
        conts.sort(key=lambda n: (-(n.get("n_decls") or 0), n["id"]))
        decls.sort(key=lambda n: n["id"])
        ordered = conts + decls
        first = [{"id": n["id"], "label": n.get("label"), "type": n["type"],
                  **({"n_decls": n["n_decls"]} if n["type"] == "container" else {}),
                  **({"n_concepts": n_concepts[n["id"]]}
                     if n["type"] == "container" and n_concepts.get(n["id"]) else {})}
                 for n in ordered[:CHILD_CAP]]
        return {"count": len(ordered), "first": first}

    # ghost decls: every snapshot decl that is NOT a brain node, listed on its
    # deepest existing container — leaf bubbles must never be silently empty
    # (a container's n_decls counts the whole snapshot; the brain only mints
    # decl NODES for declarations referenced by an ontology edge)
    ghost_names: dict[str, list[str]] = defaultdict(list)
    ghost_count: dict[str, int] = defaultdict(int)
    sf = ROOT / "catalog" / ".cache" / "statement_formal.csv"
    if sf.exists():
        seen_ghosts: set[str] = set()
        with sf.open(newline="") as fh:
            for row in csv.DictReader(fh):
                d, mod = row["decl_name"], row["module"]
                if not mod:
                    continue
                parts = mod.split(".")
                if f"decl:{parts[0]}:{d}" in nodes or d in seen_ghosts:
                    continue
                seen_ghosts.add(d)
                cur = "path:" + parts[0]
                if cur not in nodes:
                    continue
                for comp in parts[1:]:
                    if f"{cur}/{comp}" not in nodes:
                        break
                    cur = f"{cur}/{comp}"
                ghost_count[cur] += 1
                if len(ghost_names[cur]) < CHILD_CAP:
                    ghost_names[cur].append(d)
    else:
        print(f"WARNING: {sf} missing — ghost decl lists skipped "
              f"(leaf bubbles show only brain-linked decls)", file=sys.stderr)

    serialized: dict[str, str] = {}
    n_edges_attached = 0
    for nid, node in nodes.items():
        entry: dict = {"node": node}
        if node["type"] in ("container", "decl"):
            entry["breadcrumb"] = breadcrumb(nid)
        if node["type"] == "container":
            entry["children"] = child_summary(nid)
            if ghost_count.get(nid):
                entry["ghosts"] = {"count": ghost_count[nid],
                                   "first": sorted(ghost_names[nid])}
        block = {}
        for direction, per in (("out", edges_out), ("in", edges_in)):
            rows = sorted(per.get(nid, []), key=lambda t: (t[0], t[1]))
            block[direction] = [item for _, _, item in rows[:EDGE_CAP]]
        block["counts"] = {"out": len(edges_out.get(nid, [])),
                           "in": len(edges_in.get(nid, []))}
        block["truncated"] = {d: block["counts"][d] > len(block[d]) for d in ("out", "in")}
        n_edges_attached += len(block["out"]) + len(block["in"])
        entry["edges"] = block
        if nid in rollups:
            entry["rollup"] = rollups[nid]
        serialized[nid] = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

    # ---- prefix-shard the entries (build-decl-index.ts recursion) -----------
    def shard_json(ids: list[str]) -> str:
        return "{" + ",".join(f"{json.dumps(i, ensure_ascii=False)}:{serialized[i]}"
                              for i in sorted(ids)) + "}"

    leaves: dict[str, list[str]] = {}
    queue: list[tuple[int, list[str]]] = [(MIN_KEY_LEN, list(nodes))]
    while queue:
        length, ids = queue.pop()
        groups: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            groups[shard_key(i, length)].append(i)
        for key, arr in groups.items():
            if (length < MAX_KEY_LEN and len(arr) > 1
                    and len(shard_json(arr).encode()) > MAX_SHARD_BYTES):
                queue.append((length + 1, arr))
            else:
                leaves[key] = sorted(arr)

    manifest = {
        "_meta": {
            "schema": "brain/SCHEMA.md",
            # newest input generated_at, NOT build time — stable rebuilds
            "generated_at": max(m["generated_at"] for m in metas.values()),
            "inputs": {k: {"generated_at": metas[k]["generated_at"],
                           "bytes": input_files[k].stat().st_size}
                       for k in sorted(metas)},
            "licenses": metas["nodes.jsonl"]["licenses"],
            "caps": {"edges_per_direction": EDGE_CAP,
                     "rollup_per_direction": ROLLUP_CAP, "children": CHILD_CAP,
                     "evidence_trim": "depends witnesses / rollup top_witnesses "
                                      "kept to their first pair; full edges live "
                                      "in brain/data (brain/query.py)"},
            "lookup": "normalize the node id (lowercase; [a-z0-9] kept, anything "
                      "else '_'; pad with '_' to min_len), fetch <the longest key "
                      "in `shards` that prefixes it>.json, read shard[id]; edge "
                      "`prov` fields index into `prov` below",
            "counts": {"entries": len(nodes), "shards": len(leaves),
                       "ontology_edges": n_ontology,
                       "edge_attachments": n_edges_attached,
                       "rollup_attachments": n_rollup_attached},
        },
        "scheme": {"kind": "prefix", "min_len": MIN_KEY_LEN,
                   "max_len": max(len(k) for k in leaves), "max_bytes": MAX_SHARD_BYTES,
                   "pad": PAD},
        # boot payload for /brain: the level-1 drill-down starts here, no
        # per-library fetch needed until the user descends
        "roots": sorted(({"id": n["id"], "label": n.get("label"),
                          "library_kind": n.get("library_kind"),
                          "n_decls": n.get("n_decls"), "n_files": n.get("n_files")}
                         for n in nodes.values()
                         if n["type"] == "container" and "/" not in n["id"][5:]),
                        key=lambda r: -(r["n_decls"] or 0)),
        "prov": prov_list,
        "shards": {k: len(leaves[k]) for k in sorted(leaves)},
    }

    # search index: concepts + containers (decls are covered by the existing
    # decl-index shards / GET /decl); one client fetch, filtered locally
    labels = [_l for _l in (
        {"id": n["id"], "type": n["type"], "label": n.get("label"),
         **({"slug": n["slug"]} if n.get("slug") else {}),
         **({"status": n["display"]["status"]}
            if n.get("display", {}).get("status") else {}),
         **({"n_decls": n["n_decls"]} if n.get("n_decls") else {})}
        for n in nodes.values() if n["type"] in ("concept", "container"))
        if _l["label"]]
    labels.sort(key=lambda r: (r["type"], -(r.get("n_decls") or 0), r["label"]))

    # ---- atomic directory swap ----------------------------------------------
    tmp = OUT_DIR.parent / ".brain.tmp"
    old = OUT_DIR.parent / ".brain.old"
    for stale in (tmp, old):
        if stale.exists():
            shutil.rmtree(stale)
    tmp.mkdir(parents=True)
    sizes: dict[str, int] = {}
    for key in sorted(leaves):
        payload = shard_json(leaves[key]).encode()
        sizes[key] = len(payload)
        (tmp / f"{key}.json").write_bytes(payload)
    (tmp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    (tmp / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, separators=(",", ":")))
    # the transparency legend (/map's Sources view): the flattened provenance
    # registry, one entry per external database with layer + license
    reg = json.loads((ROOT / "catalog" / "data" / "source_registry.json").read_text())
    src_out = []
    def _add(key, e, group):
        src_out.append({k: e.get(k, "") for k in
                        ("name", "homepage", "layer", "kind", "our_provenance",
                         "target_license", "wikidata_property", "note")}
                       | {"key": key, "group": group})
    _add(reg["spine"]["key"], reg["spine"], "spine")
    for grp in ("node_sources", "edge_sources", "crossref_sources",
                "literature_sources", "frontier_sources", "brain_sources"):
        for k, e in reg.get(grp, {}).items():
            _add(k, e, grp)
    (tmp / "sources.json").write_text(json.dumps(
        {"layers": reg["layers"], "our_data_license": reg["our_data_license"],
         "sources": src_out}, ensure_ascii=False, separators=(",", ":")))
    if OUT_DIR.exists():
        OUT_DIR.rename(old)
    tmp.rename(OUT_DIR)
    if old.exists():
        shutil.rmtree(old)

    total = sum(sizes.values())
    largest = max(sizes, key=lambda k: sizes[k])
    hist = Counter(min(sizes[k] // 25_000, 6) for k in sizes)
    print(f"shards: {len(nodes)} entries -> {len(sizes)} shards + manifest.json "
          f"({total / 1e6:.1f} MB) in {OUT_DIR}")
    print(f"  attachments: {n_edges_attached} ontology + {n_rollup_attached} rollup")
    print("  size histogram: " + ", ".join(
        f"{'>=150K' if b == 6 else f'{b * 25}-{b * 25 + 25}K'}: {hist[b]}"
        for b in sorted(hist)))
    print(f"  largest shard: {largest}.json — {sizes[largest]} bytes; "
          f"max key length {manifest['scheme']['max_len']}")
    oversize = [k for k, s in sizes.items() if s > MAX_SHARD_BYTES]
    if oversize:
        print(f"  WARNING: {len(oversize)} shard(s) over {MAX_SHARD_BYTES} bytes "
              f"(single-entry or key-collision floors): {oversize[:5]}", file=sys.stderr)
    print(f"  wall: {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
