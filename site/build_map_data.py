#!/usr/bin/env python3
"""Unified map data builder — one artifact for the combined /map (bubbles + web).

Joins the three existing, separately-tested outputs into ONE canonical
`site/out/map_data.json` so a single page can render both the containment view
(circle-pack bubbles, formerly /atlas) and the dependency view (force-directed
web, formerly /graph) from identical node identities:

  site/out/graph_data.json        — enriched concept graph (coverage, xrefs,
                                     verified, FC frontier, conjectures) + edges
  site/out/atlas_data.json        — taxonomy assignment (continent / subfield /
                                     assign_rule), super-nodes, bubble rollups
  catalog/data/source_registry.json — provenance registry (the transparent
                                     'where every link comes from' legend)

Every node in map_data.json carries BOTH its graph enrichment AND its taxonomy
layer, so the map is layered end-to-end. Edges keep their `source`
(mathlib=formal dependency, wikidata=informal/bridge relation); the source→layer
map lives in `sources` so the web view can filter formal↔informal.

Deterministic; atomic write. Run after build_graph_page.py + build_atlas.py.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
GRAPH = OUT / "graph_data.json"
ATLAS = OUT / "atlas_data.json"
REGISTRY = HERE.parent / "catalog" / "data" / "source_registry.json"
THEOREMGRAPH = HERE.parent / "catalog" / "data" / "theoremgraph_links.json"
MAP_OUT = OUT / "map_data.json"

# Which layer each EDGE source belongs to (nodes/crossrefs carry their own).
EDGE_LAYER = {"mathlib": "formal", "wikidata": "bridge"}

# Cap decl witnesses per edge so the web view keeps tooltips without bloating
# the artifact (the full witness list stays in graph_data.json).
MAX_EDGE_DECLS = 2


def compact_sources(reg: dict) -> dict:
    """Flatten the registry into a render-ready legend: one list of sources,
    each tagged with its layer, plus the layer glossary and the edge map."""
    out = []
    def add(key: str, e: dict, group: str):
        out.append({
            "key": key, "name": e.get("name", key), "group": group,
            "layer": e.get("layer", ""), "homepage": e.get("homepage", ""),
            "wikidata_property": e.get("wikidata_property", ""),
            "url_template": e.get("url_template", ""),
            "our_provenance": e.get("our_provenance", ""),
            "target_license": e.get("target_license", ""),
            "note": e.get("note", ""),
        })
    add(reg["spine"]["key"], reg["spine"], "spine")
    for grp in ("node_sources", "edge_sources", "crossref_sources",
                "literature_sources", "frontier_sources"):
        for k, e in reg.get(grp, {}).items():
            add(k, e, grp)
    return {
        "layers": reg["layers"],
        "our_data_license": reg["our_data_license"],
        "edge_layer": EDGE_LAYER,
        "sources": out,
    }


def main() -> int:
    graph = json.loads(GRAPH.read_text())
    atlas = json.loads(ATLAS.read_text())
    reg = json.loads(REGISTRY.read_text())
    # TheoremGraph arXiv literature links (fail-soft: absent => no lit layer).
    tg = json.loads(THEOREMGRAPH.read_text())["links"] if THEOREMGRAPH.exists() else {}
    # Wikidata one-line descriptions (summaries), keyed by QID (fail-soft).
    desc_path = HERE.parent / "catalog" / "data" / "wikidata_descriptions.json"
    descriptions = json.loads(desc_path.read_text()) if desc_path.exists() else {}

    sub_continent = {k: sf["continent"] for k, sf in atlas["subfields"].items()}
    atlas_nodes = atlas["nodes"]

    nodes = []
    n_taxonomy = 0
    n_arxiv = 0
    for n in graph["nodes"]:
        q = n.get("qid")
        a = atlas_nodes.get(q) or {}
        sub = a.get("subfield", "unsorted")
        cont = sub_continent.get(sub, "unsorted")
        rule = a.get("assign_rule", "unsorted")
        if rule != "unsorted":
            n_taxonomy += 1
        nodes.append({
            **{k: n[k] for k in ("qid", "label", "slug", "primary_decl", "module",
                                 "status", "importance") if k in n},
            **({"coverage": n["coverage"]} if n.get("coverage") is not None else {}),
            **({"n_status": n["n_status"]} if n.get("n_status") is not None else {}),
            **({"n_formalized": n["n_formalized"]} if n.get("n_formalized") is not None else {}),
            **({"xrefs": n["xrefs"]} if n.get("xrefs") else {}),
            **({"verified": True} if n.get("verified") else {}),
            **({"frontier": True} if n.get("frontier") else {}),
            **({"n_conjectures": len(n["conjectures"])} if n.get("conjectures") else {}),
            **({"arxiv": tg[q]} if tg.get(q) else {}),
            **({"description": descriptions[q]} if descriptions.get(q) else {}),
            "continent": cont, "subfield": sub, "assign_rule": rule,
        })
        if tg.get(q):
            n_arxiv += 1

    edges = []
    for e in graph.get("edges", []):
        ed = {"from": e["from"], "to": e["to"], "source": e.get("source")}
        if e.get("weight"):
            ed["weight"] = e["weight"]
        if e.get("decls"):
            ed["decls"] = e["decls"][:MAX_EDGE_DECLS]
        if e.get("props"):
            ed["props"] = e["props"][:MAX_EDGE_DECLS]
        edges.append(ed)

    # "Related concepts" — each node's strongest formal neighbours (by edge
    # weight), so a node is shown IN CONTEXT (grouped with what it depends on /
    # supports) rather than as an isolated QID. Top 6 per node.
    label_by_qid = {n["qid"]: n["label"] for n in nodes}
    deg: dict[str, int] = {}
    nbr: dict[str, list[tuple[str, int]]] = {}
    for e in edges:
        w = e.get("weight", 1)
        a0, b0 = e["from"], e["to"]
        deg[a0] = deg.get(a0, 0) + 1
        deg[b0] = deg.get(b0, 0) + 1
        for a, b in ((a0, b0), (b0, a0)):
            if b in label_by_qid:
                nbr.setdefault(a, []).append((b, w))
    # Rank related by a TF-IDF-like score: weight / log2(neighbour degree). This
    # rewards STRONG + SPECIFIC edges (Matrix→Determinant, weight 197) and
    # penalises broad-but-weak connectors — e.g. "String theory" (not formalized,
    # but its article cites Group/IsSimpleGroup/… so it links to every group-
    # theory concept). A hard degree cap would wrongly drop legit hubs like
    # Matrix; normalisation keeps them. Still exclude the ~15 mega-hubs outright.
    hubs = {q for q, _ in sorted(deg.items(), key=lambda x: -x[1])[:15]}
    node_by_qid = {n["qid"]: n for n in nodes}
    # Related concepts should be formally GROUNDED (have a decl). A not-formalized
    # field-of-study like "String theory" only has edges because its article
    # cites Matrix/Group in passing — a poor "related concept". Prefer grounded
    # targets; fall back to any non-hub only if a node has too few grounded ones.
    grounded = {n["qid"] for n in nodes if n.get("primary_decl")}

    def score(b: str, w: int) -> float:
        return w / math.log2(deg.get(b, 1) + 2)

    for q, lst in nbr.items():
        cand = [(b, w) for b, w in lst if b not in hubs and b in grounded]
        if len(cand) < 3:
            cand = [(b, w) for b, w in lst if b not in hubs] or lst
        top = sorted(cand, key=lambda bw: -score(*bw))[:6]
        n = node_by_qid.get(q)
        if n is not None and top:
            n["related"] = [{"qid": b, "label": label_by_qid[b], "weight": w} for b, w in top]

    out = {
        "meta": {
            "generated_from": ["graph_data.json", "atlas_data.json", "source_registry.json"],
            "n_nodes": len(nodes), "n_edges": len(edges),
            "n_with_taxonomy": n_taxonomy, "n_with_arxiv": n_arxiv,
            "note": "Unified map artifact: nodes carry graph enrichment + taxonomy "
                    "layer; one identity across the bubble and web views.",
        },
        "sources": compact_sources(reg),
        "continents": atlas["continents"],
        "subfields": atlas["subfields"],
        "nodes": nodes,
        "supernodes": atlas["supernodes"],
        "bubble_edges": atlas["edges"],
        "edges": edges,
    }
    tmp = MAP_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(MAP_OUT)
    print(f"map_data: {len(nodes)} nodes ({n_taxonomy} taxonomy-placed, {n_arxiv} with arXiv "
          f"lit) / {len(edges)} edges / {len(atlas['supernodes'])} super-nodes / "
          f"{len(out['sources']['sources'])} sources ({MAP_OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
