#!/usr/bin/env python3
"""Build per-node neighborhood shards — SCHEMA.md's locality law as static JSON.

Reads brain/data/{nodes.jsonl, edges.jsonl, edges_links.jsonl,
rollup_edges.{module,dir}.jsonl} (edges_links.jsonl holds the split-out
kind=='links' rows — gitignored, absent ⇒ treated as empty) and writes
site/assets/brain/: prefix-named shard files, each a JSON object mapping
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

v2 additions (SCHEMA.md): ext nodes shard like any node (ids keep the
historical xref-dst string form); `f` facet bitmasks ride on labels.json rows
and children entries; labels.json gains ext rows (searchable);
views/xref_explorer.json is the global cross-ref view (facet bits 0-3 seeded,
<4 MB); aliases.json maps FQ decl names → QIDs and slugs → QID for Worker-side
unit resolution. wiki/scripts/build-public.ts wipe-then-recursive-copies the
whole directory (subdirs included) to wiki/public/assets/brain/.

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

    # ---- edges.jsonl (+ edges_links.jsonl): contains → tree maps; ontology →
    # per-node entries. The edge set ships split (build_common.write_edges):
    # edges.jsonl = every kind except `links`; edges_links.jsonl = only links
    # rows, gitignored, absent ⇒ empty. Both stream through the same consumer.
    parent: dict[str, str] = {}
    children: dict[str, list[str]] = defaultdict(list)
    edges_out: dict[str, list] = defaultdict(list)
    edges_in: dict[str, list] = defaultdict(list)
    # external-page reverse index: xref:<db>:<value> → [node ids that xref it].
    # Powers cross-pollination — GET /api/brain/edges infers xref-shared A↔B when
    # a community xref lands on a page some other node already points at.
    xref_index: dict[str, list[str]] = defaultdict(list)
    # (src, dst, kind) for the x-ref explorer view + aliases.json (v2)
    explorer_edges: list[tuple[str, str, str]] = []
    n_ontology = 0

    def consume_edges(fh) -> None:
        nonlocal n_ontology
        for line in fh:
            e = json.loads(line)
            if e["kind"] == "contains":
                parent[e["dst"]] = e["src"]
                children[e["src"]].append(e["dst"])
                continue
            n_ontology += 1
            kind = e["kind"]
            if kind in ("formalizes", "xref", "links"):
                explorer_edges.append((e["src"], e["dst"], kind))
            if kind == "xref":
                xref_index[e["dst"]].append(e["src"])
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

    with open_meta("edges.jsonl") as fh:
        consume_edges(fh)
    if (BRAIN_DATA / "edges_links.jsonl").exists():
        with open_meta("edges_links.jsonl") as fh:
            consume_edges(fh)
    else:
        print("NOTE: brain/data/edges_links.jsonl missing — links edges "
              "treated as empty (rebuild with brain/build_edges.py)",
              file=sys.stderr)

    # ---- graduated community edges (docs/BRAIN-EDITS-ROADMAP.md phase 4) ------
    # harvest_community_edges.py snapshots the live D1 tail here. Fold their xref
    # targets into the reverse index so cross-pollination sees graduated links
    # from the static base too (the live overlay still queries D1 for the tail).
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

    # ---- INFORMAL rollups: relates/mentions aggregated to container pairs ----
    # The formal tree grain alone made container levels read as "raw Mathlib
    # deps only". Here every concept gets a HOME (the container of its first
    # formalizing decl, or the folder it formalizes directly) and its relates
    # (Wikidata claims, human) + mentions (article citations, AI-moderated)
    # edges aggregate to every equal-depth ancestor pair — the human/AI synapse
    # layers drawn between bubbles at every zoom level.
    concept_home: dict[str, str] = {}
    for nid, rows in edges_out.items():
        if not nid.startswith("Q"):
            continue
        # key: two projected links edges can share (rank, dst) — via two dbs —
        # and bare tuple sort would fall through to comparing the item dicts
        for _, dst, item in sorted(rows, key=lambda t: (t[0], t[1])):
            if item["kind"] != "formalizes":
                continue
            home = dst if dst.startswith("path:") else parent.get(dst)
            if home:
                concept_home[nid] = home
                break

    def anc_chain(cid: str) -> list[str]:
        chain = [cid]
        while chain[0] in parent:
            chain.insert(0, parent[chain[0]])
        return chain

    anc_memo: dict[str, list[str]] = {}
    seen_cpairs: dict[str, set] = {}
    informal: dict[tuple[str, str], dict[str, list]] = defaultdict(dict)
    for src, rows in edges_out.items():
        if not src.startswith("Q"):
            continue
        hA = concept_home.get(src)
        if not hA:
            continue
        for _, dst, item in rows:
            if item["kind"] == "relates":
                hB = concept_home.get(dst)
            elif item["kind"] == "mentions":
                hB = parent.get(dst)
            else:
                continue
            if not hB:
                continue
            # reciprocal Wikidata claims (A→B and B→A) are ONE relationship:
            # count distinct unordered concept pairs, not directed claims
            cpair = (src, dst) if src < dst else (dst, src)
            if cpair in seen_cpairs.setdefault(item["kind"], set()):
                continue
            seen_cpairs[item["kind"]].add(cpair)
            A = anc_memo.setdefault(hA, anc_chain(hA))
            B = anc_memo.setdefault(hB, anc_chain(hB))
            for k in range(min(len(A), len(B))):
                if A[k] != B[k]:
                    for j in range(k, min(len(A), len(B))):
                        pair = (A[j], B[j]) if A[j] < B[j] else (B[j], A[j])
                        rec = informal[pair].setdefault(item["kind"], [0, []])
                        rec[0] += 1
                        if len(rec[1]) < 2:
                            dlab = (nodes[dst].get("label") if dst in nodes else None) \
                                   or dst.split(":")[-1]
                            rec[1].append([nodes[src].get("label") or src, dlab])
                    break

    inf_prov = {"relates": prov_index({"source": "wikidata_props",
                                       "method": "informal rollup (concept homes)",
                                       "pin": metas["edges.jsonl"]["generated_at"][:10]}),
                "mentions": prov_index({"source": "annotations",
                                        "method": "informal rollup (concept homes)",
                                        "pin": metas["edges.jsonl"]["generated_at"][:10]})}
    n_informal_attached = 0
    inf_rows_by_node: dict[str, list[dict]] = defaultdict(list)
    for (a, b), kinds_ in informal.items():
        for kind, (count, samples) in kinds_.items():
            row = {"kind": kind, "count": count, "samples": samples,
                   "prov": inf_prov[kind]}
            inf_rows_by_node[a].append({**row, "id": b})
            inf_rows_by_node[b].append({**row, "id": a})
    for nid, rows in inf_rows_by_node.items():
        if nid not in nodes:
            continue
        rows.sort(key=lambda r: (-r["count"], r["id"]))
        rollups[nid]["informal"] = rows[:24]
        n_informal_attached += min(len(rows), 24)

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
                     if n["type"] == "container" and n_concepts.get(n["id"]) else {}),
                  **({"f": n["f"]} if n.get("f") else {}),
                  **({"fa": n["fa"]} if n.get("fa") else {})}
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
                          "n_decls": n.get("n_decls"), "n_files": n.get("n_files"),
                          **({"f": n["f"]} if n.get("f") else {}),
                          **({"fa": n["fa"]} if n.get("fa") else {})}
                         for n in nodes.values()
                         if n["type"] == "container" and "/" not in n["id"][5:]),
                        key=lambda r: -(r["n_decls"] or 0)),
        "prov": prov_list,
        "shards": {k: len(leaves[k]) for k in sorted(leaves)},
    }

    # search index: concepts + containers + ext pages (decls are covered by the
    # existing decl-index shards / GET /decl) — PLUS the few hundred decls that
    # carry facet bits (@[wikidata]/@[stacks]/@[kerodon] tags): /api/brain/filter
    # enumerates THIS file, so tag-bit masks (f=1 etc.) must be satisfiable here
    labels = [_l for _l in (
        {"id": n["id"], "type": n["type"], "label": n.get("label"),
         **({"slug": n["slug"]} if n.get("slug") else {}),
         **({"db": n["db"]} if n["type"] == "ext" else {}),
         **({"status": n["display"]["status"]}
            if n.get("display", {}).get("status") else {}),
         **({"n_decls": n["n_decls"]} if n.get("n_decls") else {}),
         **({"f": n["f"]} if n.get("f") else {})}
        for n in nodes.values()
        if n["type"] in ("concept", "container", "ext")
        or (n["type"] == "decl" and n.get("f")))
        if _l["label"]]
    labels.sort(key=lambda r: (r["type"], -(r.get("n_decls") or 0), r["label"]))

    # ---- views/xref_explorer.json (v2): the global cross-ref explorer --------
    # Seeds = every node with a bit-0..3 facet (gold @[wikidata] / @[stacks] /
    # @[kerodon] / any xref) + the concepts/decls they connect to through
    # formalizes/xref/links edges. Deterministically trimmed under the byte
    # budget by dropping the lowest-priority edge tail (links sort last).
    SEED_MASK = 0b1111
    EXPLORER_BUDGET = 3_900_000
    kind_pri = {"formalizes": 0, "xref": 1, "links": 2}
    gen = max(m["generated_at"] for m in metas.values())
    seeds = {nid for nid, n in nodes.items() if n.get("f", 0) & SEED_MASK}
    xrows = sorted((e for e in explorer_edges if e[0] in seeds or e[1] in seeds),
                   key=lambda e: (kind_pri[e[2]], e[0], e[1]))

    def explorer_doc(rows: list, truncated: bool) -> dict:
        node_set = set(seeds)
        kept = []
        for src, dst, kind in rows:
            add, ok = [], True
            for nid in (src, dst):
                if nid in node_set:
                    continue
                n = nodes.get(nid)
                if n and n["type"] in ("concept", "decl"):
                    add.append(nid)
                else:
                    ok = False
                    break
            if not ok:
                continue
            node_set.update(add)
            kept.append({"src": src, "dst": dst, "kind": kind})
        out_nodes = []
        for nid in sorted(node_set):
            n = nodes[nid]
            out_nodes.append({
                "id": nid, "label": n.get("label"), "type": n["type"],
                **({"f": n["f"]} if n.get("f") else {}),
                **({"db": n["db"]} if n["type"] == "ext" else {}),
                **({"status": n["display"]["status"]}
                   if n.get("display", {}).get("status") else {})})
        return {"_meta": {"schema": "brain/SCHEMA.md", "generated_at": gen,
                          "seed_mask": SEED_MASK, "truncated": truncated,
                          "counts": {"nodes": len(out_nodes),
                                     "edges": len(kept)}},
                "nodes": out_nodes, "edges": kept}

    rows = xrows
    while True:
        explorer_blob = json.dumps(explorer_doc(rows, len(rows) < len(xrows)),
                                   ensure_ascii=False, separators=(",", ":"))
        if len(explorer_blob.encode()) <= EXPLORER_BUDGET or not rows:
            break
        rows = rows[: int(len(rows) * 0.8)]

    # ---- aliases.json (v2): Worker-side unit-key resolution ------------------
    decl_qids: dict[str, set] = defaultdict(set)
    for src, dst, kind in explorer_edges:
        if kind == "formalizes" and dst.startswith("decl:") and src.startswith("Q"):
            decl_qids[dst.split(":", 2)[2]].add(src)
    slug_map: dict[str, str] = {}
    for nid in sorted((n["id"] for n in nodes.values()
                       if n["type"] == "concept" and n.get("slug")),
                      key=lambda q: (len(q), q)):
        slug_map.setdefault(nodes[nid]["slug"], nid)
    aliases = {"_meta": {"schema": "brain/SCHEMA.md", "generated_at": gen,
                         "counts": {"decls": len(decl_qids),
                                    "slugs": len(slug_map)}},
               "decls": {d: sorted(qs, key=lambda q: (len(q), q))
                         for d, qs in sorted(decl_qids.items())},
               "slugs": {s: slug_map[s] for s in sorted(slug_map)}}

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
    (tmp / "views").mkdir()
    (tmp / "views" / "xref_explorer.json").write_text(explorer_blob)
    (tmp / "aliases.json").write_text(
        json.dumps(aliases, ensure_ascii=False, separators=(",", ":")))
    # external-page → nodes reverse index (cross-pollination oracle for the
    # community-edge overlay; ~150 KB, changes only on nightly rebuilds)
    (tmp / "xref_index.json").write_text(
        json.dumps({p: ns for p, ns in xref_index.items()},
                   ensure_ascii=False, separators=(",", ":")))
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
    print(f"  attachments: {n_edges_attached} ontology + {n_rollup_attached} rollup "
          f"+ {n_informal_attached} informal-rollup")
    print(f"  labels.json: {len(labels)} rows "
          f"({sum(1 for r in labels if r['type'] == 'ext')} ext); "
          f"aliases.json: {aliases['_meta']['counts']['decls']} decls, "
          f"{aliases['_meta']['counts']['slugs']} slugs")
    xm = json.loads(explorer_blob)["_meta"]
    print(f"  views/xref_explorer.json: {xm['counts']['nodes']} nodes, "
          f"{xm['counts']['edges']} edges, {len(explorer_blob.encode()) / 1e6:.1f} MB"
          f"{' (TRUNCATED to budget)' if xm['truncated'] else ''}")
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
