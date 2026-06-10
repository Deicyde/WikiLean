#!/usr/bin/env python3
"""Merge Mathlib decl->decl edges and Wikidata QID->QID edges onto a shared
QID node set.

Each output edge is tagged `source: mathlib | wikidata`. The node set is the
concept layer (so the unformalized concepts that have no Mathlib decl still
appear, isolated on the Mathlib side).

Writes:
  concept_graph.json    — {nodes, edges} for d3 / cytoscape / web viewers
  concept_graph.graphml — same graph for Gephi / yEd
And prints a diff: edges only-in-mathlib, only-in-wikidata, in-both.
"""
from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
CONCEPT = DATA / "concept_layer.jsonl"
DECL_QID = HERE / "decl_to_qid.json"
ML_EDGES = HERE / "mathlib_edges.tsv"
WD_EDGES = HERE / "wikidata_edges.jsonl"
OUT_JSON = DATA / "concept_graph.json"
OUT_GRAPHML = DATA / "concept_graph.graphml"


def main() -> None:
    decl_to_qids: dict[str, list[str]] = json.loads(DECL_QID.read_text())

    nodes: dict[str, dict] = {}
    with CONCEPT.open() as fh:
        for line in fh:
            r = json.loads(line)
            qid = r["qid"]
            titles = r.get("titles") or [qid]
            nodes[qid] = {
                "qid": qid,
                "label": r.get("primary_title") or titles[0],
                "slug": r.get("article_slug"),
                "primary_decl": r.get("primary_decl"),
                "module": r.get("module"),
                "status": r.get("status"),
                "importance": r.get("importance"),
            }

    ml: dict[tuple[str, str], list[list[str]]] = {}
    if ML_EDGES.exists():
        with ML_EDGES.open() as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                a, b = line.split("\t", 1)
                for qa in decl_to_qids.get(a, []):
                    for qb in decl_to_qids.get(b, []):
                        if qa == qb:
                            continue
                        ml.setdefault((qa, qb), []).append([a, b])

    wd: dict[tuple[str, str], list[dict]] = {}
    if WD_EDGES.exists():
        with WD_EDGES.open() as fh:
            for line in fh:
                r = json.loads(line)
                s, o = r["s"], r["o"]
                if s == o or s not in nodes or o not in nodes:
                    continue
                wd.setdefault((s, o), []).append(
                    {"p": r["p"], "label": r.get("p_label", "")}
                )

    edges: list[dict] = []
    for (a, b), decls in ml.items():
        edges.append({"from": a, "to": b, "source": "mathlib", "decls": decls})
    for (a, b), props in wd.items():
        edges.append({"from": a, "to": b, "source": "wikidata", "props": props})

    OUT_JSON.write_text(
        json.dumps({"nodes": list(nodes.values()), "edges": edges}, indent=2)
    )

    with OUT_GRAPHML.open("w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n')
        for key, name in [
            ("label", "label"), ("module", "module"),
            ("status", "status"), ("decl", "primary_decl"),
        ]:
            fh.write(
                f'  <key id="{key}" for="node" attr.name="{name}" '
                f'attr.type="string"/>\n'
            )
        fh.write('  <key id="src" for="edge" attr.name="source" attr.type="string"/>\n')
        fh.write('  <key id="prop" for="edge" attr.name="prop" attr.type="string"/>\n')
        fh.write('  <graph id="WikiLean" edgedefault="directed">\n')
        for n in nodes.values():
            fh.write(f'    <node id="{n["qid"]}">\n')
            fh.write(f'      <data key="label">{escape(n["label"] or "")}</data>\n')
            fh.write(f'      <data key="module">{escape(n["module"] or "")}</data>\n')
            fh.write(f'      <data key="status">{escape(n["status"] or "")}</data>\n')
            fh.write(f'      <data key="decl">{escape(n["primary_decl"] or "")}</data>\n')
            fh.write('    </node>\n')
        for i, e in enumerate(edges):
            fh.write(
                f'    <edge id="e{i}" source="{e["from"]}" target="{e["to"]}">\n'
            )
            fh.write(f'      <data key="src">{e["source"]}</data>\n')
            if e["source"] == "wikidata" and e.get("props"):
                fh.write(f'      <data key="prop">{escape(e["props"][0]["p"])}</data>\n')
            fh.write('    </edge>\n')
        fh.write('  </graph>\n</graphml>\n')

    ml_keys = set(ml)
    wd_keys = set(wd)
    print(f"nodes: {len(nodes)}")
    print(
        f"mathlib edges: {len(ml_keys)}  "
        f"wikidata edges: {len(wd_keys)}  "
        f"both: {len(ml_keys & wd_keys)}"
    )
    print(f"  only mathlib:  {len(ml_keys - wd_keys)}")
    print(f"  only wikidata: {len(wd_keys - ml_keys)}")
    print(f"-> {OUT_JSON}")
    print(f"-> {OUT_GRAPHML}")


if __name__ == "__main__":
    main()
