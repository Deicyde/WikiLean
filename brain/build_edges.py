#!/usr/bin/env python3
"""Build brain/data/edges.jsonl — every BRAIN edge per brain/SCHEMA.md.

Kinds: contains (containment tree + decl placement), formalizes (concept→decl
post-override; concept→container when container_links.jsonl exists), mentions
(annotation-cited decls — excluded from formalization-status logic), depends
(QID→QID lifted Mathlib deps), relates (Wikidata P-props), xref (concept→
external DB, values in evidence), cites (concept→literature via TheoremGraph
links + the transitive join over grounded decls), matches (decl→literature).
Every edge carries {kind, provenance:{source, method, pin}, confidence,
evidence}; first line is a {"_meta": ...} block.

Run: python3 brain/build_edges.py
"""
from __future__ import annotations

from pathlib import Path

from build_common import BRAIN_DATA, build, write_jsonl

OUT = BRAIN_DATA / "edges.jsonl"


def main() -> int:
    _nodes, edges, meta = build()
    write_jsonl(OUT, meta, edges)
    counts = meta["counts"]["edges"]
    print(f"edges: {sum(counts.values())} total "
          f"({', '.join(f'{k}={v}' for k, v in counts.items())}) "
          f"-> {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT} "
          f"({OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
