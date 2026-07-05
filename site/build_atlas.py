#!/usr/bin/env python3
"""Bubble-atlas hierarchy builder (Phase A of the multilayer map).

Consumes site/out/graph_data.json (the fully-enriched concept graph: coverage,
crossrefs, verified, FC frontier) and the curated taxonomy, and emits
site/out/atlas_data.json: continents → subfields → concepts (+ decl
super-nodes), with aggregated inter-bubble edges.

Deterministic assignment, best evidence first — and every node records WHICH
rule placed it (assign_rule), the evidence-payload doctrine applied to the
hierarchy itself:
  1. module   — Mathlib module root (the community's own curated taxonomy)
  2. msc      — the node's MSC crossref chip (P3285, 2-digit)
  3. ams      — FormalConjectures @[AMS] codes on the node's conjectures
  4. neighbors— one-pass plurality vote over graph edges to assigned nodes
                (strict winner with ≥2 votes required)
  5. unsorted — the honest bucket; never guessed

Super-nodes: decls carrying ≥2 catalog QIDs (Module = Vector space + Module +
Scalar multiplication + …) group their members WITHIN a subfield — members in
different subfields stay separate (a bubble never tears across the taxonomy).

Run after build_graph_page.py; nightly pushes the output to KV atlas:data:v1.
"""
import collections
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRAPH = HERE / "out" / "graph_data.json"
TAXONOMY = HERE.parent / "catalog" / "data" / "atlas_taxonomy.json"
CATALOG_GLOB = sorted((HERE.parent / "catalog" / "data").glob("*_tagged.jsonl"))
OUT = HERE / "out" / "atlas_data.json"

FC_TAGGED = HERE.parent / "catalog" / "data" / "fc_tagged.jsonl"


def slugify(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def main() -> int:
    tax = json.loads(TAXONOMY.read_text())
    graph = json.loads(GRAPH.read_text())
    nodes = graph["nodes"]
    by_qid = {n["qid"]: n for n in nodes if n.get("qid")}

    # FC AMS codes per QID (rule 3 evidence).
    fc_ams: dict[str, set[str]] = collections.defaultdict(set)
    if FC_TAGGED.exists():
        for line in FC_TAGGED.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                fc_ams[r["qid"]].update(c for c in (r.get("ams") or []) if c)

    mod_tab = tax["module_root_subfields"]
    msc_tab = tax["msc_subfields"]

    def classify(n: dict) -> tuple[str, str, str] | None:
        """→ (subfield_label, continent, rule) or None (needs vote/unsorted)."""
        m = n.get("module") or ""
        if m.startswith("Mathlib.") and m.split(".")[1] in mod_tab:
            e = mod_tab[m.split(".")[1]]
            return e["subfield"], e["continent"], "module"
        for code in (n.get("xrefs") or {}).get("msc", []):
            two = str(code)[:2]
            if two in msc_tab:
                e = msc_tab[two]
                return e["subfield"], e["continent"], "msc"
        for code in sorted(fc_ams.get(n.get("qid") or "", ())):
            two = str(code).zfill(2)[:2]
            if two in msc_tab:
                e = msc_tab[two]
                return e["subfield"], e["continent"], "ams"
        return None

    assigned: dict[str, tuple[str, str, str]] = {}
    for n in nodes:
        c = classify(n)
        if c:
            assigned[n["qid"]] = c

    # Rule 4: one-pass neighbor plurality (strict winner, ≥2 votes).
    adj: dict[str, list[str]] = collections.defaultdict(list)
    for e in graph.get("edges", []):
        adj[e["from"]].append(e["to"])
        adj[e["to"]].append(e["from"])
    for n in nodes:
        q = n["qid"]
        if q in assigned:
            continue
        votes = collections.Counter(
            assigned[nb][0] for nb in adj.get(q, []) if nb in assigned)
        if votes:
            (top, cnt), *rest = votes.most_common(2) + [(None, 0)] * 1
            if cnt >= 2 and cnt > rest[0][1]:
                cont = next(v["continent"] for v in list(mod_tab.values()) + list(msc_tab.values())
                            if v["subfield"] == top)
                assigned[q] = (top, cont, "neighbors")

    # Build subfield → members; unassigned → Unsorted.
    subfields: dict[str, dict] = {}
    node_out: dict[str, dict] = {}
    for n in nodes:
        q = n["qid"]
        sub, cont, rule = assigned.get(q) or ("Unsorted", "unsorted", "unsorted")
        key = slugify(sub)
        sf = subfields.setdefault(key, {"key": key, "label": sub, "continent": cont, "qids": []})
        sf["qids"].append(q)
        node_out[q] = {
            "label": n.get("label") or "", "slug": n.get("slug") or "",
            "status": n.get("status") or "", "subfield": key, "assign_rule": rule,
            **({"coverage": n["coverage"]} if n.get("coverage") is not None else {}),
            **({"verified": True} if n.get("verified") else {}),
            **({"frontier": True} if n.get("frontier") else {}),
            **({"n_conjectures": len(n["conjectures"])} if n.get("conjectures") else {}),
        }

    # Super-nodes: decl → member QIDs (≥2), same-subfield only.
    decl_qids: dict[str, set[str]] = collections.defaultdict(set)
    for f in CATALOG_GLOB:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            d, q = r.get("primary_decl"), r.get("wikidata_qid")
            if d and q and q in node_out:
                decl_qids[d].add(q)
    supernodes = []
    for d, qs in sorted(decl_qids.items()):
        by_sub = collections.defaultdict(list)
        for q in qs:
            by_sub[node_out[q]["subfield"]].append(q)
        for sub, members in by_sub.items():
            if len(members) >= 2:
                supernodes.append({"decl": d, "subfield": sub, "members": sorted(members)})

    # Aggregated edges: subfield-pair + continent-pair rollups with examples.
    pair_stats: dict[tuple[str, str], dict] = {}
    for e in graph.get("edges", []):
        a, b = e["from"], e["to"]
        if a not in node_out or b not in node_out:
            continue
        sa, sb = node_out[a]["subfield"], node_out[b]["subfield"]
        if sa == sb:
            continue
        k = tuple(sorted((sa, sb)))
        st = pair_stats.setdefault(k, {"count": 0, "mathlib": 0, "wikidata": 0, "examples": []})
        st["count"] += 1
        st[e.get("source") or "wikidata"] = st.get(e.get("source") or "wikidata", 0) + 1
        if len(st["examples"]) < 3:
            st["examples"].append({"from": a, "to": b, "source": e.get("source"),
                                   "decls": (e.get("decls") or [])[:2]})
    sub_edges = [{"a": k[0], "b": k[1], **v} for k, v in
                 sorted(pair_stats.items(), key=lambda kv: -kv[1]["count"])]
    cont_pairs: dict[tuple[str, str], int] = collections.defaultdict(int)
    for ed in sub_edges:
        ca = subfields[ed["a"]]["continent"]
        cb = subfields[ed["b"]]["continent"]
        if ca != cb:
            cont_pairs[tuple(sorted((ca, cb)))] += ed["count"]

    out = {
        "continents": [
            {**c, "subfields": sorted(k for k, s in subfields.items() if s["continent"] == c["key"]),
             "n_concepts": sum(len(s["qids"]) for s in subfields.values() if s["continent"] == c["key"])}
            for c in tax["continents"]
        ],
        "subfields": subfields,
        "nodes": node_out,
        "supernodes": supernodes,
        "edges": {"subfield_pairs": sub_edges,
                  "continent_pairs": [{"a": a, "b": b, "count": n} for (a, b), n in
                                      sorted(cont_pairs.items(), key=lambda kv: -kv[1])]},
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(OUT)
    rules = collections.Counter(v["assign_rule"] for v in node_out.values())
    print(f"atlas: {len(node_out)} concepts → {len(subfields)} subfields / "
          f"{len(tax['continents'])} continents; rules {dict(rules)}; "
          f"{len(supernodes)} super-nodes; {len(sub_edges)} subfield edge-pairs "
          f"({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
