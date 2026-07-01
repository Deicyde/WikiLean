#!/usr/bin/env python3
"""Build the reverse citation index for /decl/:name — decl → every WikiLean
statement citing it. Deterministic; reads site/annotations/*.json (the disk
mirror the nightly keeps in sync with D1 — same freshness tradeoff as the
concept-graph refresh, and refreshed in the same nightly block).

Output: site/out/decl_citations.json  {decl: [{slug, id, label, status}, …]}
The nightly pushes it to KV as `declcites:v1` (`wrangler kv key put`, no Worker
deploy); wiki/src/decl.ts serves it on JSON requests.

  python3 site/build_decl_citations.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANNOT = HERE / "annotations"
OUT = HERE / "out" / "decl_citations.json"


def main() -> int:
    cites: dict[str, list[dict]] = {}
    n_articles = 0
    for f in sorted(ANNOT.glob("*.json")):
        if f.name.endswith(".agent1.json") or f.name.startswith("."):
            continue
        try:
            doc = json.loads(f.read_text())
        except Exception:
            continue  # a partial write must not sink the whole index
        slug = doc.get("slug") or f.stem
        n_articles += 1
        for a in doc.get("annotations") or []:
            if a.get("status") not in ("formalized", "partial"):
                continue
            decl = (a.get("mathlib") or {}).get("decl")
            aid = a.get("id")
            if not (isinstance(decl, str) and decl and isinstance(aid, str)):
                continue
            cites.setdefault(decl, []).append({
                "slug": slug, "id": aid,
                "label": (a.get("label") or "")[:120],
                "status": a["status"],
            })
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(cites, ensure_ascii=False))
    n_cites = sum(len(v) for v in cites.values())
    print(f"decl_citations.json: {len(cites)} decls, {n_cites} citations "
          f"from {n_articles} articles ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
