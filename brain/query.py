#!/usr/bin/env python3
"""BRAIN query CLI — the local (agent-facing) read surface over brain/data.

Fast path: the prefix shards in site/assets/brain (one file read per node,
same artifact the live /brain UI fetches). Full path: brain/data/*.jsonl for
untruncated edge lists. JSON to stdout, always.

  python3 brain/query.py node <id>                 shard entry (payload, edges,
                                                   breadcrumb, children, rollup)
  python3 brain/query.py neighborhood <id> [--kinds formalizes,xref]
                                           [--full]  untruncated (scans edges.jsonl)
  python3 brain/query.py path <id>                 containment breadcrumb only
  python3 brain/query.py search <text> [--type concept|container|decl]
                                                   label substring over nodes.jsonl

Node ids per brain/SCHEMA.md: Q181296 | path:Mathlib/CategoryTheory |
decl:Mathlib:CommGroup | lit:<arxiv>#<ref>.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SHARDS = HERE.parent / "site" / "assets" / "brain"


def shard_key(node_id: str, length: int) -> str:
    k = ""
    for i in range(length):
        if i < len(node_id):
            c = node_id[i].lower()
            k += c if ("a" <= c <= "z" or "0" <= c <= "9") else "_"
        else:
            k += "_"
    return k


def shard_entry(node_id: str) -> dict | None:
    mf = SHARDS / "manifest.json"
    if not mf.exists():
        return None
    manifest = json.loads(mf.read_text())
    shards, scheme = manifest["shards"], manifest["scheme"]
    lo, hi = scheme["min_len"], scheme["max_len"]
    for length in range(min(hi, max(len(node_id), lo)), lo - 1, -1):
        k = shard_key(node_id, length)
        if k in shards:
            entry = json.loads((SHARDS / f"{k}.json").read_text()).get(node_id)
            if entry is not None:
                entry["_prov_table"] = manifest["prov"]
            return entry
    for length in range(max(len(node_id), lo) + 1, hi + 1):  # padded upward retry
        k = shard_key(node_id, length)
        if k in shards:
            entry = json.loads((SHARDS / f"{k}.json").read_text()).get(node_id)
            if entry is not None:
                entry["_prov_table"] = manifest["prov"]
            return entry
    return None


def iter_jsonl(path: Path):
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if "_meta" not in r:
                yield r


def cmd_node(args) -> int:
    e = shard_entry(args.id)
    if e is None:
        print(json.dumps({"ok": False, "error": "unknown node id", "id": args.id}))
        return 1
    print(json.dumps({"ok": True, **e}, ensure_ascii=False))
    return 0


def cmd_neighborhood(args) -> int:
    kinds = set(args.kinds.split(",")) if args.kinds else None
    if not args.full:
        e = shard_entry(args.id)
        if e is not None:
            block = e.get("edges", {})
            if kinds:
                block = {**block,
                         "out": [x for x in block.get("out", []) if x["kind"] in kinds],
                         "in": [x for x in block.get("in", []) if x["kind"] in kinds]}
            print(json.dumps({"ok": True, "id": args.id, "edges": block,
                              "rollup": e.get("rollup"),
                              "_prov_table": e.get("_prov_table")}, ensure_ascii=False))
            return 0
    out, inn = [], []
    for r in iter_jsonl(DATA / "edges.jsonl"):
        if kinds and r["kind"] not in kinds:
            continue
        if r["src"] == args.id:
            out.append(r)
        elif r["dst"] == args.id:
            inn.append(r)
    if not out and not inn:
        print(json.dumps({"ok": False, "error": "no edges (or unknown id)", "id": args.id}))
        return 1
    print(json.dumps({"ok": True, "id": args.id,
                      "edges": {"out": out, "in": inn,
                                "counts": {"out": len(out), "in": len(inn)},
                                "truncated": {"out": False, "in": False}}},
                     ensure_ascii=False))
    return 0


def cmd_path(args) -> int:
    e = shard_entry(args.id)
    if e is None:
        print(json.dumps({"ok": False, "error": "unknown node id", "id": args.id}))
        return 1
    print(json.dumps({"ok": True, "id": args.id,
                      "breadcrumb": e.get("breadcrumb", [])}, ensure_ascii=False))
    return 0


def cmd_search(args) -> int:
    q = args.text.casefold()
    hits = []
    for n in iter_jsonl(DATA / "nodes.jsonl"):
        if args.type and n.get("type") != args.type:
            continue
        label = (n.get("label") or "")
        if q in label.casefold() or q in (n.get("slug") or "").casefold():
            hits.append({"id": n["id"], "type": n["type"], "label": label,
                         **({"status": n["display"]["status"]}
                            if n.get("display", {}).get("status") else {})})
            if len(hits) >= args.limit:
                break
    print(json.dumps({"ok": True, "query": args.text, "hits": hits}, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("node"); p.add_argument("id"); p.set_defaults(fn=cmd_node)
    p = sub.add_parser("neighborhood"); p.add_argument("id")
    p.add_argument("--kinds"); p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_neighborhood)
    p = sub.add_parser("path"); p.add_argument("id"); p.set_defaults(fn=cmd_path)
    p = sub.add_parser("search"); p.add_argument("text")
    p.add_argument("--type", choices=["concept", "container", "decl", "literature"])
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_search)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
