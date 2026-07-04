#!/usr/bin/env python3
"""Build brain/data/nodes.jsonl — the BRAIN node set per brain/SCHEMA.md.

Node types: concept (Wikidata QID layer), container (hierarchy.json file-tree),
decl (only decls referenced by >=1 ontology edge), literature (arXiv id + ref).
First line is a {"_meta": ...} block (generated_at from newest input mtime,
input pins, license attributions); every following line is one node.

Run: python3 brain/build_nodes.py
"""
from __future__ import annotations

from pathlib import Path

from build_common import BRAIN_DATA, build, write_jsonl

OUT = BRAIN_DATA / "nodes.jsonl"


def main() -> int:
    nodes, _edges, meta = build()
    write_jsonl(OUT, meta, nodes)
    counts = meta["counts"]["nodes"]
    print(f"nodes: {sum(counts.values())} total "
          f"({', '.join(f'{k}={v}' for k, v in counts.items())}) "
          f"-> {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT} "
          f"({OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
