#!/usr/bin/env python3
"""Build brain/data/edges.jsonl + edges_links.jsonl — every BRAIN edge per
brain/SCHEMA.md, split across two files (GitHub's 100 MB per-file hard limit).

edges.jsonl carries every kind EXCEPT `links`: contains (containment tree +
decl placement), formalizes (concept→decl post-override; concept→container
when container_links.jsonl exists), mentions (annotation-cited decls —
excluded from formalization-status logic), depends (QID→QID lifted Mathlib
deps), relates (Wikidata P-props), xref (concept→external DB, values in
evidence), cites (concept→literature via TheoremGraph links + the transitive
join over grounded decls), matches (decl→literature). Its first line is the
FULL build {"_meta": ...} (counts span both files).

edges_links.jsonl carries only kind=='links' rows (external-DB page→page
hyperlinks + concept projections) with a small _meta of its own. It is
GITIGNORED — deterministically rebuilt from the committed
catalog/data/external/ inputs; readers treat a missing file as empty.

Every edge carries {kind, provenance:{source, method, pin}, confidence,
evidence}.

Run: python3 brain/build_edges.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from build_common import EDGES_LINKS_OUT, EDGES_OUT, build, write_edges

# stay comfortably under GitHub's 100 MB hard limit for the COMMITTED file
MAX_COMMITTED_BYTES = 95 * 1024 * 1024


def _rel(p: Path) -> Path:
    return p.relative_to(Path.cwd()) if p.is_relative_to(Path.cwd()) else p


def main() -> int:
    _nodes, edges, meta = build()
    n = write_edges(edges, meta)
    counts = meta["counts"]["edges"]
    print(f"edges: {sum(counts.values())} total "
          f"({', '.join(f'{k}={v}' for k, v in counts.items())})")
    print(f"  {_rel(EDGES_OUT)}: {n['main']} rows (all kinds except links, "
          f"{EDGES_OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  {_rel(EDGES_LINKS_OUT)}: {n['links']} rows (kind=links only, "
          f"gitignored, {EDGES_LINKS_OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    if EDGES_OUT.stat().st_size >= MAX_COMMITTED_BYTES:
        print(f"WARNING: {EDGES_OUT.name} is "
              f"{EDGES_OUT.stat().st_size / 1024 / 1024:.1f} MB — approaching "
              f"GitHub's 100 MB per-file hard limit; split another kind out "
              f"before committing", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
