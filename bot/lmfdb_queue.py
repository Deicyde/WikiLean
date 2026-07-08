#!/usr/bin/env python3
"""Build an LMFDB crossref queue from the Brain graph.

The signal is deterministic:
  concept QID --xref:lmfdb_knowl--> LMFDB knowl id
  concept QID --formalizes--> Mathlib declaration

The output uses the database-agnostic queue shape consumed by publish_queue.py
and the Worker queue/review UI:
  {"db":"lmfdb","id":"group.abelian","concept_qid":"Q181296","decl":...,"file":...}
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EDGES = ROOT / "brain" / "data" / "edges.jsonl"
NODES = ROOT / "brain" / "data" / "nodes.jsonl"
CENTRALITY = ROOT / "manage" / "data" / "centrality.json"
TAG_XREFS = ROOT / "catalog" / "data" / "mathlib_tag_xrefs.jsonl"
OUT = HERE / "state" / "lmfdb_queue.json"

CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and not row.get("_meta"):
            rows.append(row)
    return rows


def load_nodes() -> tuple[dict[str, dict], dict[str, dict]]:
    concepts: dict[str, dict] = {}
    decls: dict[str, dict] = {}
    for row in read_jsonl(NODES):
        node_id = row.get("id")
        if not isinstance(node_id, str):
            continue
        if node_id.startswith("Q") and node_id[1:].isdigit():
            concepts[node_id] = row
        elif node_id.startswith("decl:"):
            decls[node_id] = row
    return concepts, decls


def load_centrality() -> dict[str, float]:
    if not CENTRALITY.exists():
        return {}
    try:
        scores = json.loads(CENTRALITY.read_text()).get("scores", {})
    except Exception:
        return {}
    return {q: float(r.get("centrality_pct", 0.0)) for q, r in scores.items()}


def module_to_file(module: str | None) -> str | None:
    if not module or not module.startswith("Mathlib."):
        return None
    return module.replace(".", "/") + ".lean"


def already_tagged_lmfdb() -> set[str]:
    tagged: set[str] = set()
    for row in read_jsonl(TAG_XREFS):
        if row.get("db") == "lmfdb" and isinstance(row.get("tag"), str):
            tagged.add(row["tag"])
    return tagged


def parse_formalizes(edge: dict) -> tuple[str | None, str | None]:
    src, dst = edge.get("src"), edge.get("dst")
    if isinstance(src, str) and isinstance(dst, str):
        if src.startswith("Q") and src[1:].isdigit() and dst.startswith("decl:Mathlib:"):
            return src, dst
        if dst.startswith("Q") and dst[1:].isdigit() and src.startswith("decl:Mathlib:"):
            return dst, src
    return None, None


def confidence(edge: dict) -> str:
    c = edge.get("confidence")
    return c if isinstance(c, str) and c in CONF_RANK else "medium"


def worst_confidence(*values: str) -> str:
    return max(values, key=lambda c: CONF_RANK.get(c, 1))


def allowed(conf: str, min_confidence: str) -> bool:
    return CONF_RANK.get(conf, 1) <= CONF_RANK[min_confidence]


def build(
    source: Path = EDGES,
    include_seen: bool = False,
    min_confidence: str = "medium",
    limit: int | None = None,
) -> list[dict]:
    concepts, decls = load_nodes()
    centrality = load_centrality()
    tagged = set() if include_seen else already_tagged_lmfdb()
    xrefs: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    formalizes: dict[str, list[tuple[str, dict]]] = defaultdict(list)

    for edge in read_jsonl(source):
        if edge.get("kind") == "xref":
            src, dst = edge.get("src"), edge.get("dst")
            if isinstance(src, str) and src.startswith("Q") and isinstance(dst, str) and dst.startswith("xref:lmfdb_knowl:"):
                ident = dst.split(":", 2)[2]
                if ident not in tagged:
                    xrefs[src].append((ident, edge))
        elif edge.get("kind") == "formalizes":
            qid, decl_node = parse_formalizes(edge)
            if qid and decl_node:
                formalizes[qid].append((decl_node, edge))

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    order = 0
    for qid, knowls in xrefs.items():
        for decl_node, fedge in formalizes.get(qid, []):
            f_conf = confidence(fedge)
            if not allowed(f_conf, min_confidence):
                continue
            decl_meta = decls.get(decl_node, {})
            ev = fedge.get("evidence") if isinstance(fedge.get("evidence"), dict) else {}
            module = ev.get("module") or decl_meta.get("module") or fedge.get("module")
            file = module_to_file(module)
            if not file:
                continue
            decl = decl_node.split(":", 2)[2]
            for ident, xedge in knowls:
                x_conf = confidence(xedge)
                combined = worst_confidence(f_conf, x_conf)
                if not allowed(combined, min_confidence):
                    continue
                pair = (ident, decl)
                if pair in seen:
                    continue
                seen.add(pair)
                item = {
                    "db": "lmfdb",
                    "id": ident,
                    "concept_qid": qid,
                    "label": concepts.get(qid, {}).get("label", ident),
                    "decl": decl,
                    "file": file,
                    "status": "brain",
                    "source": "brain-lmfdb-xref",
                    "priority_source": "brain",
                    "provenance_tier": "wikidata-p12987+brain-formalizes",
                    "brain_node": qid,
                    "decl_node": decl_node,
                    "confidence": combined,
                    "review_reason": "LMFDB knowl from Wikidata P12987 joined to a Brain formalizes edge",
                    "_order": order,
                }
                if qid in centrality:
                    item["centrality_pct"] = centrality[qid]
                items.append(item)
                order += 1

    def key(item: dict) -> tuple:
        return (
            CONF_RANK.get(item.get("confidence"), 1),
            -float(item.get("centrality_pct", 0.0)),
            item["_order"],
            item["id"],
            item["decl"],
        )

    items.sort(key=key)
    clean = [{k: v for k, v in item.items() if not k.startswith("_") and v is not None} for item in items]
    return clean[:limit] if limit is not None else clean


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="source", type=Path, default=EDGES,
                    help="Brain edge JSONL (default: brain/data/edges.jsonl)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--include-seen", action="store_true",
                    help="include LMFDB ids already harvested from Mathlib (debugging only)")
    ap.add_argument("--min-confidence", choices=sorted(CONF_RANK, key=CONF_RANK.get),
                    default="medium")
    ap.add_argument("--dry-run", action="store_true", help="print payload instead of writing")
    args = ap.parse_args()

    items = build(
        source=args.source,
        include_seen=args.include_seen,
        min_confidence=args.min_confidence,
        limit=args.limit,
    )
    if args.dry_run:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        print(f"{len(items)} LMFDB queue item(s)", file=sys.stderr)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {args.out} ({len(items)} LMFDB queue item(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
