#!/usr/bin/env python3
"""Re-run "old" articles through the current v3 annotation pipeline.

DEPRECATED: superseded by moderate.py review (D1-direct). Kept for one-off
schema migrations.

Two earlier generations are weaker than the current schema-v3 pipeline (see the
quality comparison): the `annotations_v2/` set (schema_version 1 — ~4x sparser,
no kind/label/match_kind/provenance) and articles that were rendered but whose
annotation JSON no longer exists (data survives only embedded in out/<slug>.html).

This driver finds those articles, re-runs each through the v3 pipeline
(batch_annotate.annotate_one: fetch → extract → Agent 1 → validate → Agent 2 →
render), and overwrites annotations/<slug>.json with a fresh schema-v3 record.
It is **resumable**: a slug already at schema_version 3 in annotations/ is skipped.

Run with the venv that has claude-agent-sdk:
    catalog/.venv/bin/python site/update_old_annotations.py --dry-run
    catalog/.venv/bin/python site/update_old_annotations.py --concurrency 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.parse
from pathlib import Path

import batch_annotate as ba  # reuse the orchestrator's pipeline + run loop

HERE = Path(__file__).resolve().parent
ANNOT = HERE / "annotations"
ANNOT_V2 = HERE / "annotations_v2"
OUT = HERE / "out"

_WIKILINK_RE = re.compile(
    r'href="https://en\.wikipedia\.org/wiki/([^"?]+)"[^>]*>view on Wikipedia')
NON_ARTICLE = {"index", "concepts", "about", "404"}


def is_v3(slug: str) -> bool:
    """True if annotations/<slug>.json already exists at schema_version 3."""
    p = ANNOT / f"{slug}.json"
    if not p.exists():
        return False
    try:
        return json.loads(p.read_text()).get("schema_version") == 3
    except (json.JSONDecodeError, OSError):
        return False


def title_from_html(slug: str) -> str | None:
    """Recover the exact Wikipedia title from a rendered page's permalink."""
    p = OUT / f"{slug}.html"
    if not p.exists():
        return None
    m = _WIKILINK_RE.search(p.read_text())
    return urllib.parse.unquote(m.group(1)).replace("_", " ") if m else None


def find_old_articles() -> list[dict]:
    """Slugs whose current annotation is NOT schema-v3, with their titles.
    Source 1: annotations_v2/*.json (schema v1, has wikipedia_title).
    Source 2: rendered out/*.html with no annotations/<slug>.json at all."""
    found: dict[str, str] = {}  # slug -> title

    if ANNOT_V2.exists():
        for f in ANNOT_V2.glob("*.json"):
            slug = f.stem
            if is_v3(slug):
                continue
            try:
                d = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            title = d.get("wikipedia_title") or slug.replace("_", " ")
            found[slug] = title

    for h in OUT.glob("*.html"):
        slug = h.stem
        if slug in NON_ARTICLE or slug in found or is_v3(slug):
            continue
        if (ANNOT / f"{slug}.json").exists():
            continue  # has some annotations/ json (not v3 handled above; v1 elsewhere)
        title = title_from_html(slug)
        if title:
            found[slug] = title

    return [{"title": t} for t in sorted(set(found.values()))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="List the articles that would be re-run, then exit.")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--moderate", action="store_true",
                    help="Context-aware: load current annotations, preserve human "
                         "edits, review/extend rather than clobber. Use once articles "
                         "have human contributions (e.g. from the live wiki).")
    args = ap.parse_args()

    if not ba.MATHLIB.exists():
        print(f"ERROR: mathlib4 not found at {ba.MATHLIB}", file=sys.stderr)
        return 1

    articles = find_old_articles()
    if args.limit:
        articles = articles[: args.limit]

    print(f"old articles needing a v3 re-run: {len(articles)}")
    for a in articles[:20]:
        print(f"  {a['title']}")
    if len(articles) > 20:
        print(f"  … +{len(articles) - 20} more")
    if args.dry_run or not articles:
        return 0

    _, seed_decls = ba.load_articles()
    # ba.run reprocesses every article it's given (the skip-if-rendered logic
    # lives in ba.main, not ba.run), so it force-updates these in place.
    return asyncio.run(ba.run(articles, seed_decls, args.concurrency,
                              moderate=args.moderate))


if __name__ == "__main__":
    raise SystemExit(main())
