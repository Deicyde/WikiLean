#!/usr/bin/env python3
"""MathWorld ingest (ids-only, NO snippets, NO links — Wolfram ToS forbids
crawling and IP-blocks scrapers; article pages are NEVER fetched).

Pages come purely from Wikidata P2812 crossrefs (CC0): slug ids, titles derived
by CamelCase splitting, deep-link urls. One sitemap.xml request records the
total slug inventory in _meta only (sanctioned one-time inventory check).

Run: python3 brain/ingest/mathworld.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

SITEMAP = "https://mathworld.wolfram.com/sitemap.xml"
PAGE_URL = "https://mathworld.wolfram.com/{}.html"
# insert spaces at CamelCase boundaries; hyphens/digit runs left intact
CAMEL1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
CAMEL2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def slug_title(slug: str) -> str:
    return CAMEL2.sub(" ", CAMEL1.sub(" ", slug))


def sitemap_inventory() -> int | None:
    """One polite request, cached ~monthly; None when unavailable (IP block)."""
    cache = common.cache_path("mathworld", "sitemap.xml")
    if common.stale(cache, 24 * 28):
        try:
            cache.write_bytes(common.curl_fetch(SITEMAP))
        except Exception as e:  # noqa: BLE001 — inventory is _meta-only, optional
            print(f"[mathworld] sitemap fetch failed: {e}", file=sys.stderr)
    if not cache.exists():
        return None
    return cache.read_text(errors="replace").count("<loc>")


def main() -> int:
    qids = common.qid_map("mathworld")
    pages = [{"db": "mathworld", "id": slug, "title": slug_title(slug),
              "url": PAGE_URL.format(slug), "qid": qid}
             for slug, qid in sorted(qids.items())]
    common.emit("mathworld", pages, [], extra_meta={
        "source_pin": "wikidata P2812 via catalog/data/wikidata_crossrefs.json",
        "sitemap_inventory": sitemap_inventory(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
