#!/usr/bin/env python3
"""BRAIN query CLI — the local (agent-facing) read surface over brain/data.

BRAIN v3: the addressable thing is a **cell** — an atom of organs — not a
particle. A Mathlib decl, a Wikidata concept, an external-DB page, a WikiLean
article and an arXiv statement that denote ONE object are ORGANS of one cell
(`cell:Q18848` holds Q18848 "module", Q125977 "vector space" AND
decl:Mathlib:Module). Mathlib folders are SUPERCELLS (`path:…`) that own
field-of-study concepts. Weak bonds between two atoms aggregate into one
SYNAPSE carrying every trace. Contract: brain/SCHEMA.md#v3.

Fast path: the prefix shards in site/assets/brain/cells (one file read per
atom, the same artifact /brain and the live API read). Full path:
brain/data/{cells,synapses}.jsonl — the shards trim each synapse to 6 traces
(`traces_total` says how many exist) and cap the synapse list itself, so
`--full` is how you get the untruncated set. JSON to stdout, always.

  python3 brain/query.py cell <key>                ANY organ id → the owning
                                                   atom's card (organs with
                                                   embedded payloads, synapse
                                                   summary, breadcrumb)
  python3 brain/query.py organs <key> [--kind decl|concept|page|article|statement]
  python3 brain/query.py neighborhood <key> [--kinds depends,links]
                                            [--full]  untruncated synapses +
                                                   FULL traces (scans
                                                   synapses.jsonl)
  python3 brain/query.py path <key>                containment breadcrumb only
  python3 brain/query.py search <text> [--type cell|supercell]
                                                   label + `aka` (organ-label)
                                                   substring search
  python3 brain/query.py supercell <path>          a Mathlib folder: its organs,
                                                   child folders and cells

`unit` and `node` remain as aliases of `cell` (the v2 unit card BECAME the cell
card; v3 has no particle nodes). Exit 1 on a miss.

Keys: any organ id — Q181296 | decl:Mathlib:CommGroup | xref:<db>:<value> |
an article slug | lit:<arxiv>#<ref> — or an atom id: cell:<anchor> | path:<Lib>/<Dir>.
aliases.json maps every organ to its atom; that is why every pre-v3 id still resolves.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SHARDS = HERE.parent / "site" / "assets" / "brain" / "cells"
QID_RE = re.compile(r"Q\d+")


def shard_key(atom_id: str, length: int) -> str:
    k = ""
    for i in range(length):
        if i < len(atom_id):
            c = atom_id[i].lower()
            k += c if ("a" <= c <= "z" or "0" <= c <= "9") else "_"
        else:
            k += "_"
    return k


def _load(name: str) -> dict | list | None:
    p = SHARDS / name
    return json.loads(p.read_text()) if p.exists() else None


def shard_entry(atom_id: str) -> dict | None:
    """One cell entry, via the manifest's documented prefix scheme."""
    manifest = _load("manifest.json")
    if not manifest:
        return None
    shards, scheme = manifest["shards"], manifest["scheme"]
    lo, hi = scheme["min_len"], scheme["max_len"]
    for length in range(min(hi, max(len(atom_id), lo)), lo - 1, -1):
        k = shard_key(atom_id, length)
        if k in shards:
            entry = json.loads((SHARDS / f"{k}.json").read_text()).get(atom_id)
            if entry is not None:
                entry["_prov_table"] = manifest["prov"]
            return entry
    for length in range(max(len(atom_id), lo) + 1, hi + 1):  # padded upward retry
        k = shard_key(atom_id, length)
        if k in shards:
            entry = json.loads((SHARDS / f"{k}.json").read_text()).get(atom_id)
            if entry is not None:
                entry["_prov_table"] = manifest["prov"]
            return entry
    return None


def supercell_entry(path: str) -> dict | None:
    """One supercell (Mathlib folder). Supercells live in supercells.json, NOT
    in the cell shards — a rule-5 field concept resolves here, not to a cell."""
    f = _load("supercells.json")
    if not f:
        return None
    e = f.get("supercells", {}).get(path)
    if e is None:
        return None
    crumbs, seen, cur = [], set(), e.get("parent")
    while cur and cur not in seen:  # supercells.json is a tree; derive the breadcrumb
        seen.add(cur)
        parent = f["supercells"].get(cur, {})
        crumbs.append({"id": cur, "label": parent.get("label")})
        cur = parent.get("parent")
    return {"id": path, "kind": "supercell", "breadcrumb": list(reversed(crumbs)), **e}


def iter_jsonl(path: Path):
    with path.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if "_meta" not in r:
                yield r


def resolve_key(key: str) -> str | None:
    """ANY organ id (or atom id) → the atom that owns it.

    Fast path: site/assets/brain/cells/aliases.json — THE compat layer, and a
    function (SCHEMA C4: every organ resolves to exactly one atom). Slow path:
    scan cells.jsonl, so the command works before the shards are built.
    """
    if key.startswith(("cell:", "path:")):
        return key
    aliases = _load("aliases.json")
    if aliases:
        for table in ("organs", "decls", "slugs"):
            hit = aliases.get(table, {}).get(key)
            if hit:
                return hit
        if key.startswith("decl:"):  # bare-name index, for a foreign library prefix
            hit = aliases.get("decls", {}).get(key.split(":", 2)[2])
            if hit:
                return hit
        # aliases exist but miss: fall through — the shards may lag the data
    cells = DATA / "cells.jsonl"
    if not cells.exists():
        return None
    name = key.split(":", 2)[2] if key.startswith("decl:") else key
    for c in iter_jsonl(cells):
        for o in c.get("organs", []):
            if o["id"] == key or (o["kind"] == "decl" and o["id"].split(":", 2)[2] == name):
                return c["id"]
        if c.get("label") == key:
            return c["id"]
    # rule 5: a field concept is an organ of a SUPERCELL, never of a cell
    meta, _ = _first_meta(cells)
    for path, organs in (meta.get("supercell_organs") or {}).items():
        if any(o["id"] == key for o in organs):
            return path
    return None


def _first_meta(path: Path) -> tuple[dict, None]:
    with path.open() as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                return (r.get("_meta", {}), None) if "_meta" in r else ({}, None)
    return {}, None


def atom(key: str) -> tuple[str, dict] | None:
    """(atom id, entry) for any key, or None."""
    aid = resolve_key(key)
    if aid is None:
        return None
    e = supercell_entry(aid) if aid.startswith("path:") else shard_entry(aid)
    return (aid, e) if e is not None else None


def _fail(msg: str, **kw) -> int:
    print(json.dumps({"ok": False, "error": msg, **kw}))
    return 1


def cmd_cell(args) -> int:
    got = atom(args.key)
    if got is None:
        return _fail("unresolvable key (no atom owns it)", key=args.key)
    aid, e = got
    print(json.dumps({"ok": True, "key": args.key, "id": aid, **e}, ensure_ascii=False))
    return 0


def cmd_organs(args) -> int:
    got = atom(args.key)
    if got is None:
        return _fail("unresolvable key (no atom owns it)", key=args.key)
    aid, e = got
    organs = e.get("organs", [])
    if args.kind:
        organs = [o for o in organs if o["kind"] == args.kind]
    print(json.dumps({"ok": True, "key": args.key, "id": aid, "organs": organs,
                      "counts": e.get("counts")}, ensure_ascii=False))
    return 0


def cmd_neighborhood(args) -> int:
    kinds = set(args.kinds.split(",")) if args.kinds else None
    got = atom(args.key)
    if got is None:
        return _fail("unresolvable key (no atom owns it)", key=args.key)
    aid, e = got

    if not args.full:
        syn = [s for s in e.get("syn", [])
               if not kinds or (set(s.get("kinds", {})) & kinds)]
        if kinds:  # filter the traces too — asking for `depends` must not dump `links`
            syn = [{**s, "traces": [t for t in s.get("traces", []) if t["kind"] in kinds]}
                   for s in syn]
        print(json.dumps({"ok": True, "id": aid, "synapses": syn,
                          "returned": len(syn),
                          "counts": e.get("counts"),
                          # the shard trims traces per synapse (`tt` = the true
                          # total) AND caps the list itself — --full lifts both
                          "truncated": e.get("truncated"),
                          "_prov_table": e.get("_prov_table")}, ensure_ascii=False))
        return 0

    # --full: the untruncated set, straight from the atom layer.
    src = DATA / "synapses.jsonl"
    if not src.exists():
        return _fail("brain/data/synapses.jsonl absent — run brain/build_cells.py", id=aid)
    out = []
    for s in iter_jsonl(src):
        if s["src"] != aid and s["dst"] != aid:
            continue
        if kinds and not (set(s.get("kinds", {})) & kinds):
            continue
        traces = s.get("traces", [])
        if kinds:
            traces = [t for t in traces if t["kind"] in kinds]
        out.append({"id": s["dst"] if s["src"] == aid else s["src"],
                    "w": s["weight"], "kinds": s["kinds"], "traces": traces,
                    **({"truncated": s["truncated"]} if s.get("truncated") else {})})
    out.sort(key=lambda s: (-s["w"], s["id"]))
    if not out:
        return _fail("no synapses (or unknown atom)", id=aid)
    meta, _ = _first_meta(src)
    print(json.dumps({"ok": True, "id": aid, "synapses": out, "returned": len(out),
                      "full": True, "_prov_table": meta.get("prov")},
                     ensure_ascii=False))
    return 0


def cmd_path(args) -> int:
    got = atom(args.key)
    if got is None:
        return _fail("unresolvable key (no atom owns it)", key=args.key)
    aid, e = got
    print(json.dumps({"ok": True, "id": aid, "breadcrumb": e.get("breadcrumb", []),
                      "supercells": e.get("cell", {}).get("supercells")},
                     ensure_ascii=False))
    return 0


def cmd_supercell(args) -> int:
    path = args.path if args.path.startswith("path:") else f"path:{args.path}"
    e = supercell_entry(path)
    if e is None:
        return _fail("unknown supercell", path=path)
    print(json.dumps({"ok": True, **e}, ensure_ascii=False))
    return 0


def cmd_search(args) -> int:
    """Label + `aka` search. An atom is named by its ANCHOR, so the organ labels
    in `aka` are often the only handle a caller holds — "vector space" has to
    find the Module atom. Prefix hits rank before substring hits (mirroring
    searchLabels in wiki/src/brain.ts), or the ranking is file order and the
    exact match loses to whatever mentions the phrase first."""
    q = args.text.casefold()
    starts, contains = [], []

    def add(row: dict, names: list[str]) -> None:
        names = [n.casefold() for n in names if n]
        if any(n.startswith(q) for n in names):
            starts.append(row)
        elif any(q in n for n in names):
            contains.append(row)

    if args.type != "supercell":
        for r in _load("labels.json") or []:
            add(r, [r.get("label") or ""] + list(r.get("aka") or []))
    if args.type == "supercell" or not args.type:
        f = _load("supercells.json") or {}
        for path, e in (f.get("supercells") or {}).items():
            add({"id": path, "label": e.get("label"), "kind": "supercell"},
                [e.get("label") or ""] + [o.get("label") or "" for o in e.get("organs") or []])
    hits = (starts + contains)[: args.limit]
    print(json.dumps({"ok": True, "query": args.text, "hits": hits}, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    # `unit`/`node` are v2 entry points kept alive: the unit card became the cell
    # card, and v3 has no particle nodes — an organ id resolves to its atom.
    for name in ("cell", "unit", "node"):
        p = sub.add_parser(name)
        p.add_argument("key")
        p.set_defaults(fn=cmd_cell)
    p = sub.add_parser("organs"); p.add_argument("key")
    p.add_argument("--kind", choices=["concept", "decl", "page", "article", "statement"])
    p.set_defaults(fn=cmd_organs)
    p = sub.add_parser("neighborhood"); p.add_argument("key")
    p.add_argument("--kinds"); p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_neighborhood)
    p = sub.add_parser("path"); p.add_argument("key"); p.set_defaults(fn=cmd_path)
    p = sub.add_parser("supercell"); p.add_argument("path"); p.set_defaults(fn=cmd_supercell)
    p = sub.add_parser("search"); p.add_argument("text")
    p.add_argument("--type", choices=["cell", "supercell"])
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_search)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
