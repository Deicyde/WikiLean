#!/usr/bin/env python3
"""Assemble the rebuilt concept graph (v2) from the agent grounding.

Reads the parallel-agent grounding (verified Mathlib formalizations per concept),
applies a DETERMINISTIC decl-existence backstop (the agents proposed + a skeptic
verified; here the oracle/checkout has the final say — anti-slop), builds the
upgraded node set (multi-library formalizations; primary_decl = best), lifts the
FULL formal dependency graph to QID→QID edges via lift_formal_edges, folds in the
Wikidata relation edges, and writes concept_graph_v2.json + a diff vs the live
graph. Does NOT touch the canonical graph — this is a reviewable artifact.

Usage: python3 catalog/build_graph_v2.py --grounding catalog/data/rebuild_grounding.json
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LIVE = DATA / "concept_graph.json"
ORACLE = HERE.parent / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"
CHECKOUT = Path("/Users/jack/Desktop/LEAN/mathlib4/Mathlib")
WD_EDGES = HERE / "mathlib_deps" / "wikidata_edges.jsonl"
ANNOT = HERE.parent / "site" / "annotations"
OUT = DATA / "concept_graph_v2.json"
D2Q_OUT = DATA / "decl_to_qid_v2.json"

sys.path.insert(0, str(HERE))
import lift_formal_edges  # noqa: E402


def oracle_names() -> set[str]:
    try:
        d = json.loads(ORACLE.read_text())
        return set(d.get("declarations", {}))
    except (OSError, json.JSONDecodeError):
        return set()


def checkout_has(decls: list[str]) -> set[str]:
    """Backstop for oracle gaps: grep the live checkout for each decl's defining
    line (last-segment match on a decl keyword). Returns those found."""
    found: set[str] = set()
    if not CHECKOUT.exists():
        return found
    kw = r"(theorem|lemma|def|abbrev|structure|class|instance|inductive)"
    for d in decls:
        seg = d.split(".")[-1]
        try:
            r = subprocess.run(["grep", "-rIlE", f"{kw} +{seg}\\b", str(CHECKOUT)],
                               capture_output=True, text=True, timeout=30)
            if r.stdout.strip():
                found.add(d)
        except (subprocess.SubprocessError, OSError):
            pass
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grounding", type=Path, default=DATA / "rebuild_grounding.json")
    args = ap.parse_args()

    concepts = json.loads(args.grounding.read_text())
    if isinstance(concepts, dict):
        concepts = concepts.get("concepts", [])
    live = json.loads(LIVE.read_text())
    live_by_qid = {n["qid"]: n for n in live["nodes"]}
    live_edge_n = len(live["edges"])
    # Authoritative labels — the agents left `label` null for expansion nodes, so
    # resolve from the target list / universe, never fall back to a bare QID.
    tgt_label: dict[str, str] = {}
    tf = HERE.parent / "manage" / "data" / "rebuild_targets.json"
    if tf.exists():
        for r in json.loads(tf.read_text()):
            if r.get("label"):
                tgt_label[r["qid"]] = r["label"]

    def humanize(slug: str | None) -> str:
        return (slug or "").replace("_", " ") if slug else ""

    # ---- deterministic decl-existence filter --------------------------------
    proposed = {f["decl"] for c in concepts for f in (c.get("formalizations") or [])}
    okset = oracle_names()
    misses = sorted(proposed - okset)
    print(f"proposed decls: {len(proposed)} | in oracle: {len(proposed) - len(misses)} | "
          f"oracle-misses to checkout-verify: {len(misses)}", file=sys.stderr)
    rescued = checkout_has(misses) if misses else set()
    valid = okset | rescued
    dropped = [d for d in misses if d not in rescued]
    print(f"  rescued from checkout: {len(rescued)} | DROPPED (nonexistent): {len(dropped)}",
          file=sys.stderr)

    # ---- build v2 nodes ------------------------------------------------------
    nodes = []
    decl_to_qid: dict[str, list[str]] = collections.defaultdict(list)
    n_new = n_gained_decl = n_changed_decl = 0
    for c in concepts:
        qid = c["qid"]
        forms = [f for f in (c.get("formalizations") or []) if f["decl"] in valid]
        prev = live_by_qid.get(qid)
        primary = forms[0]["decl"] if forms else None
        module = forms[0].get("module") if forms else (prev or {}).get("module")
        status = "formalized" if any(f["match_kind"] == "exact" and f.get("confidence") == "high" for f in forms) \
            else ("partial" if forms else "not_formalized")
        # keep the agent's status if it's more conservative and forms exist
        if c.get("status") in ("formalized", "partial", "not_formalized") and forms:
            status = c["status"] if not (status == "formalized" and c["status"] == "partial") else status
        node = {
            "qid": qid,
            "label": c.get("label") or tgt_label.get(qid) or (prev or {}).get("label")
                     or humanize(c.get("slug")) or qid,
            "slug": c.get("slug") or (prev or {}).get("slug"),
            "primary_decl": primary,
            "module": module,
            "status": status if forms else "not_formalized",
            "importance": (prev or {}).get("importance") or "Mid",
            "formalizations": [{k: f.get(k) for k in ("decl", "module", "library", "match_kind", "confidence")}
                               for f in forms],
            "xrefs_keys": c.get("xrefs") or [],
            "arxiv": c.get("arxiv") or [],
            "is_new": qid not in live_by_qid,
        }
        nodes.append(node)
        for f in forms:
            decl_to_qid[f["decl"]].append(qid)
        if node["is_new"]:
            n_new += 1
        elif not prev.get("primary_decl") and primary:
            n_gained_decl += 1
        elif prev.get("primary_decl") and primary and prev["primary_decl"] != primary:
            n_changed_decl += 1

    node_qids = {n["qid"] for n in nodes}

    # Densify the EDGE decl→QID map with every decl an article CITES (mapped to
    # that article's concept QID). These are real, curated decl-usages; folding
    # them in lets the full dep-graph lift many more formally-grounded edges than
    # the grounding's primary decls alone. (Node formalizations stay the curated
    # high-quality set; this only feeds edge lifting.) grounding ∪ annotations.
    slug2qid = {n["slug"]: n["qid"] for n in nodes if n.get("slug")}
    import glob
    n_ann_decls = 0
    for f in glob.glob(str(ANNOT / "*.json")):
        if f.endswith(".agent1.json"):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        qid = slug2qid.get(d.get("slug"))
        if not qid:
            continue
        for a in (d.get("annotations") or []):
            dec = (a.get("mathlib") or {}).get("decl")
            if dec:
                decl_to_qid[dec].append(qid)
                n_ann_decls += 1
    decl_to_qid = {k: sorted(set(v)) for k, v in decl_to_qid.items()}
    print(f"  decl→QID map for edges: {len(decl_to_qid)} decls "
          f"(grounding + {n_ann_decls} annotation citations)", file=sys.stderr)
    D2Q_OUT.write_text(json.dumps(decl_to_qid, ensure_ascii=False))

    # ---- edges: full formal dep-graph lift + wikidata relations -------------
    formal_edges = lift_formal_edges.lift(decl_to_qid)
    formal_edges = [e for e in formal_edges if e["from"] in node_qids and e["to"] in node_qids]
    wd_edges = []
    if WD_EDGES.exists():
        seen = set()
        for line in WD_EDGES.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            s, o = r.get("s"), r.get("o")
            if s in node_qids and o in node_qids and s != o and (s, o) not in seen:
                seen.add((s, o))
                wd_edges.append({"from": s, "to": o, "source": "wikidata",
                                 "props": [{"p": r.get("p"), "label": r.get("p_label", "")}]})
    edges = formal_edges + wd_edges

    OUT.write_text(json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False))

    # ---- diff / stats --------------------------------------------------------
    n_formalized = sum(1 for n in nodes if n["status"] == "formalized")
    n_with_decl = sum(1 for n in nodes if n["primary_decl"])
    n_with_arxiv = sum(1 for n in nodes if n["arxiv"])
    live_formalized = sum(1 for n in live["nodes"] if n.get("status") == "formalized")
    live_with_decl = sum(1 for n in live["nodes"] if n.get("primary_decl"))
    print("\n===== concept_graph_v2 =====")
    print(f"nodes: {len(nodes)}  (was {len(live['nodes'])}; +{n_new} new)")
    print(f"with a formalization: {n_with_decl}  (was {live_with_decl})")
    print(f"status=formalized: {n_formalized}  (was {live_formalized})")
    print(f"edges: {len(edges)}  (was {live_edge_n})  [formal {len(formal_edges)} + wikidata {len(wd_edges)}]")
    print(f"decls newly grounded on existing nodes: +{n_gained_decl} | primary_decl changed: {n_changed_decl}")
    print(f"nodes with arXiv literature: {n_with_arxiv}")
    print(f"decl-existence: dropped {len(dropped)} nonexistent decl(s) as a backstop")
    print(f"\nwrote {OUT.name} ({OUT.stat().st_size/1024:.0f} KB) + {D2Q_OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
