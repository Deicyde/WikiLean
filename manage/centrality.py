#!/usr/bin/env python3
"""Concept-graph centrality — the structural half of WikiLean's control plane.

Reads catalog/data/concept_graph.json (QID nodes; directed edges from Mathlib
decl-dependencies + Wikidata semantic relations) and writes
manage/data/centrality.json: a per-QID structural-importance score.

A Mathlib edge ``A -> B`` means "decl for A references decl for B", i.e. A
depends on B, so PageRank flows toward foundational concepts (Set, Group, …) —
which is exactly the priority signal we want the pipeline and the moderation
loop to inherit. Degree measures are reported alongside for transparency.

Pure Python (no networkx), deterministic, no network, no LLM. Safe to run on
the deterministic side of the repo; touches nothing the bot owns.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
GRAPH = ROOT / "catalog" / "data" / "concept_graph.json"
OUT = HERE / "data" / "centrality.json"


def load_graph(path: Path = GRAPH):
    """Return (nodes: qid->node, edges: list[(from, to, source)]).

    Edges are kept only when both endpoints are real nodes (the graph is
    already filtered this way upstream, but we don't trust it blindly)."""
    d = json.loads(Path(path).read_text())
    nodes = {n["qid"]: n for n in d.get("nodes", []) if n.get("qid")}
    edges = [
        (e["from"], e["to"], e.get("source"))
        for e in d.get("edges", [])
        if e.get("from") in nodes and e.get("to") in nodes and e["from"] != e["to"]
    ]
    return nodes, edges


def pagerank(qids, edges, damping: float = 0.85, iters: int = 200, tol: float = 1e-10):
    """Standard PageRank on the directed graph, with dangling-node handling."""
    n = len(qids)
    if n == 0:
        return {}
    idx = {q: i for i, q in enumerate(qids)}
    out_links = [[] for _ in range(n)]
    outdeg = [0] * n
    for f, t, _ in edges:
        out_links[idx[f]].append(idx[t])
        outdeg[idx[f]] += 1
    pr = [1.0 / n] * n
    base = (1.0 - damping) / n
    for _ in range(iters):
        dangling = sum(pr[i] for i in range(n) if outdeg[i] == 0)
        dshare = damping * dangling / n
        nxt = [base + dshare] * n
        for i in range(n):
            if outdeg[i]:
                share = damping * pr[i] / outdeg[i]
                for j in out_links[i]:
                    nxt[j] += share
        diff = sum(abs(nxt[i] - pr[i]) for i in range(n))
        pr = nxt
        if diff < tol:
            break
    return {q: pr[idx[q]] for q in qids}


def degrees(qids, edges):
    """In/out/total degree overall and Mathlib-only in-degree (foundational-ness)."""
    z = {q: {"in": 0, "out": 0, "in_mathlib": 0} for q in qids}
    for f, t, src in edges:
        z[f]["out"] += 1
        z[t]["in"] += 1
        if src == "mathlib":
            z[t]["in_mathlib"] += 1
    return z


def _percentiles(score: dict) -> dict:
    """Map each qid to its 0-100 percentile by score (100 = most central).

    Percentile = fraction of nodes with a *strictly lower* score, so nodes with
    identical scores collapse to one value. (In the live graph ~655 leaf nodes
    share the uniform PageRank floor; an index-based rank would spread them
    across 0-47pct by arbitrary JSON order — an order-dependent priority signal.)
    """
    import bisect

    n = len(score)
    if n <= 1:
        return {q: 100.0 for q in score}
    vals = sorted(score.values())
    return {q: round(100.0 * bisect.bisect_left(vals, score[q]) / (n - 1), 2) for q in score}


def compute(path: Path = GRAPH) -> dict:
    nodes, edges = load_graph(path)
    qids = list(nodes)
    pr = pagerank(qids, edges)
    deg = degrees(qids, edges)
    pct = _percentiles(pr)
    out = {}
    for q in qids:
        n = nodes[q]
        out[q] = {
            "qid": q,
            "label": n.get("label"),
            "slug": n.get("slug"),
            "status": n.get("status"),
            "importance": n.get("importance"),
            "pagerank": round(pr[q], 8),
            "centrality_pct": pct[q],
            "in_degree": deg[q]["in"],
            "out_degree": deg[q]["out"],
            "in_degree_mathlib": deg[q]["in_mathlib"],
        }
    return {
        "n_nodes": len(qids),
        "n_edges": len(edges),
        "scores": out,
    }


def main() -> None:
    result = compute()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    top = sorted(result["scores"].values(), key=lambda r: -r["pagerank"])[:12]
    print(f"centrality: {result['n_nodes']} nodes, {result['n_edges']} edges -> {OUT.relative_to(ROOT)}")
    print("most central concepts (PageRank):")
    for r in top:
        print(f"  {r['centrality_pct']:6.2f}pct  in={r['in_degree']:<4} {r['qid']:<11} {r['label']}")


if __name__ == "__main__":
    main()
