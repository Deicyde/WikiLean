#!/usr/bin/env python3
"""BRAIN query CLI — the local (agent-facing) read surface over brain/data.

Fast path: the prefix shards in site/assets/brain (one file read per node,
same artifact the live /brain UI fetches). Full path: brain/data/*.jsonl for
untruncated edge lists — the edge set is split across edges.jsonl (every kind
except `links`) and edges_links.jsonl (only kind=='links'; gitignored, absent
⇒ treated as empty), and full scans merge both transparently. JSON to stdout,
always.

  python3 brain/query.py node <id>                 shard entry (payload, edges,
                                                   breadcrumb, children, rollup)
  python3 brain/query.py neighborhood <id> [--kinds formalizes,xref]
                                           [--full]  untruncated (scans
                                                   edges.jsonl + edges_links.jsonl)
  python3 brain/query.py path <id>                 containment breadcrumb only
  python3 brain/query.py search <text> [--type concept|container|decl|ext]
                                                   label substring over nodes.jsonl
  python3 brain/query.py unit <key>                resolve QID | decl:Lib:Name |
                                                   bare decl name | slug |
                                                   xref:db:id → the owning
                                                   concept's node payload (incl.
                                                   its `unit` card); exit 1 on miss

Node ids per brain/SCHEMA.md: Q181296 | path:Mathlib/CategoryTheory |
decl:Mathlib:CommGroup | lit:<arxiv>#<ref> | xref:<db>:<value>.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SHARDS = HERE.parent / "site" / "assets" / "brain"
QID_RE = re.compile(r"Q\d+")


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


def iter_edges():
    """Every edge row: edges.jsonl (all kinds except links) merged with
    edges_links.jsonl (only kind=='links'; absent ⇒ empty)."""
    yield from iter_jsonl(DATA / "edges.jsonl")
    links = DATA / "edges_links.jsonl"
    if links.exists():
        yield from iter_jsonl(links)


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
                out = [x for x in block.get("out", []) if x["kind"] in kinds]
                inn = [x for x in block.get("in", []) if x["kind"] in kinds]
                # counts/truncated must describe the FILTERED lists — the
                # shard's totals span all kinds and would contradict them.
                # After a kind filter the true per-kind total is unknowable
                # from a truncated shard, so flag truncation only when the
                # shard itself was truncated in that direction.
                block = {"out": out, "in": inn,
                         "counts": {"out": len(out), "in": len(inn)},
                         "counts_all_kinds": block.get("counts"),
                         "truncated": block.get("truncated")}
            print(json.dumps({"ok": True, "id": args.id, "edges": block,
                              "rollup": e.get("rollup"),
                              "_prov_table": e.get("_prov_table")}, ensure_ascii=False))
            return 0
    out, inn = [], []
    for r in iter_edges():
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


def _first_qid(qids) -> str | None:
    """Deterministic owner pick: lowest QID number first (aliases.json order)."""
    qs = sorted({q for q in qids if isinstance(q, str) and QID_RE.fullmatch(q)},
                key=lambda q: (len(q), q))
    return qs[0] if qs else None


def resolve_unit_key(key: str) -> str | None:
    """QID | decl:Lib:Name | bare decl name | slug | xref:db:id → owning QID.

    Fast path: site/assets/brain/{aliases,xref_index}.json (built by
    build_shards.py). Slow path: scan brain/data (formalizes in-edges for
    decls, node slugs, xref edges) so the command works pre-shards too.
    """
    if QID_RE.fullmatch(key):
        return key
    if key.startswith("xref:"):
        p = SHARDS / "xref_index.json"          # ext page → [xref-ing node ids]
        if p.exists():
            q = _first_qid(json.loads(p.read_text()).get(key, []))
            if q:
                return q
        return _first_qid(r["src"] for r in iter_edges()
                          if r["kind"] == "xref" and r["dst"] == key)
    name = key.split(":", 2)[2] if key.startswith("decl:") else key
    p = SHARDS / "aliases.json"
    if p.exists():
        aliases = json.loads(p.read_text())
        q = _first_qid(aliases.get("decls", {}).get(name, []))
        if q:
            return q
        if not key.startswith("decl:"):
            q = aliases.get("slugs", {}).get(key)
            if q:
                return q
        # aliases exist but miss: fall through — the shards may lag the data
    q = _first_qid(
        r["src"] for r in iter_edges()
        if r["kind"] == "formalizes" and r["dst"].startswith("decl:")
        and (r["dst"] == key if key.startswith("decl:")
             else r["dst"].split(":", 2)[2] == name))
    if q:
        return q
    if not key.startswith("decl:"):
        for n in iter_jsonl(DATA / "nodes.jsonl"):
            if n.get("type") == "concept" and n.get("slug") == key:
                return n["id"]
    return None


def cmd_unit(args) -> int:
    key = args.key.strip()
    qid = resolve_unit_key(key)
    if qid is None:
        print(json.dumps({"ok": False, "error": "unresolvable unit key",
                          "key": key}))
        return 1
    e = shard_entry(qid)
    node = e.get("node") if e else None
    if node is None:                            # shards absent or lagging
        node = next((n for n in iter_jsonl(DATA / "nodes.jsonl")
                     if n["id"] == qid), None)
    if node is None:
        print(json.dumps({"ok": False, "error": "resolved QID has no node payload",
                          "key": key, "qid": qid}))
        return 1
    print(json.dumps({"ok": True, "key": key, "qid": qid, "node": node},
                     ensure_ascii=False))
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
    p.add_argument("--type", choices=["concept", "container", "decl",
                                      "literature", "ext"])
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("unit"); p.add_argument("key"); p.set_defaults(fn=cmd_unit)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
