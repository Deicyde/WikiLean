#!/usr/bin/env python3
"""Build bot/state/brain_queue.json from graduated Brain formalizes edges.

This is a deterministic suggestion lane for the @[wikidata] review queue. It
does not apply tags and does not bypass Mathlib PR review: it only converts
validated Brain concept -> Mathlib decl edges into the same small `{qid,file,decl}`
shape that open_batch.py can choose from.

Input is intentionally the graduated static Brain layer, not the live edit table:
`brain/harvest_community_edges.py` is responsible for validating/graduating live
D1 brain_edges first. Human community edges are trusted after endpoint validation;
AI community edges have already passed the harvester's oracle.

  python3 bot/brain_queue.py --dry-run
  python3 bot/brain_queue.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pool

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMMUNITY_EDGES = ROOT / "brain" / "data" / "community_edges.jsonl"
NODES = ROOT / "brain" / "data" / "nodes.jsonl"
CENTRALITY = ROOT / "manage" / "data" / "centrality.json"
OUT = HERE / "state" / "brain_queue.json"


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
    """Return concept metadata and decl metadata keyed by Brain node id."""
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


def parse_pair(edge: dict) -> tuple[str | None, str | None]:
    """Return (qid, decl_node) for either concept->decl or decl->concept edges."""
    src, dst = edge.get("src"), edge.get("dst")
    if isinstance(src, str) and isinstance(dst, str):
        if src.startswith("Q") and src[1:].isdigit() and dst.startswith("decl:Mathlib:"):
            return src, dst
        if dst.startswith("Q") and dst[1:].isdigit() and src.startswith("decl:Mathlib:"):
            return dst, src
    return None, None


def tagged_qids() -> set[str]:
    if not pool.TAGGED.exists():
        return set()
    return {l.strip() for l in pool.TAGGED.read_text().splitlines() if l.strip().startswith("Q")}


def build(source: Path = COMMUNITY_EDGES, include_seen: bool = False, limit: int | None = None) -> list[dict]:
    concepts, decls = load_nodes()
    centrality = load_centrality()
    excluded = set() if include_seen else (pool.seen_qids() | tagged_qids())
    items, seen_pairs = [], set()

    for idx, edge in enumerate(read_jsonl(source)):
        if edge.get("kind") != "formalizes":
            continue
        qid, decl_node = parse_pair(edge)
        if not qid or not decl_node or qid in excluded:
            continue
        decl_meta = decls.get(decl_node, {})
        module = decl_meta.get("module") or edge.get("module")
        file = module_to_file(module)
        if not file:
            continue
        decl = decl_node.split(":", 2)[2]
        pair = (qid, decl)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        ev = edge.get("evidence") if isinstance(edge.get("evidence"), dict) else {}
        actor = ev.get("actor_type") or "human"
        added_by = ev.get("added_by")
        tier = "community-human" if actor == "human" else "community-ai-verified"
        centrality_pct = centrality.get(qid)
        reason = "Brain community formalizes edge"
        if actor:
            reason += f" ({actor})"
        if added_by:
            reason += f" by {added_by}"
        item = {
            "qid": qid,
            "label": concepts.get(qid, {}).get("label", ""),
            "decl": decl,
            "file": file,
            "status": "brain",
            "source": "brain-community",
            "provenance_tier": tier,
            "priority_source": "brain",
            "brain_node": qid,
            "decl_node": decl_node,
            "brain_edge_id": ev.get("edge_id"),
            "actor_type": actor,
            "added_by": added_by,
            "review_reason": reason,
            "_order": idx,
        }
        if isinstance(centrality_pct, float):
            item["centrality_pct"] = centrality_pct
        if edge.get("confidence"):
            item["confidence"] = edge.get("confidence")
        items.append(item)

    def key(item: dict) -> tuple:
        actor_rank = 0 if item.get("actor_type") == "human" else 1
        return (actor_rank, -float(item.get("centrality_pct", 0.0)), item["_order"], item["qid"], item["decl"])

    items.sort(key=key)
    clean = [{k: v for k, v in item.items() if not k.startswith("_") and v is not None} for item in items]
    return clean[:limit] if limit is not None else clean


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="source", type=Path, default=COMMUNITY_EDGES,
                    help="Brain formalizes-edge JSONL (default: brain/data/community_edges.jsonl)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--include-seen", action="store_true",
                    help="include QIDs that were already seen/tagged (debugging only)")
    ap.add_argument("--dry-run", action="store_true", help="print payload instead of writing")
    args = ap.parse_args()

    items = build(source=args.source, include_seen=args.include_seen, limit=args.limit)
    if args.dry_run:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        print(f"{len(items)} Brain queue item(s)", file=sys.stderr)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {args.out} ({len(items)} Brain queue item(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
