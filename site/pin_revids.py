#!/usr/bin/env python3
"""Pin each cached article to the Wikipedia revision it was annotated against.

Every cached cache/<slug>.html was fetched at some moment (the file's mtime).
This backfills a sidecar cache/<slug>.meta.json recording the revid that was
LIVE at that moment — i.e. the revision our annotations were actually made for
— recovered via the MediaWiki API (revision-as-of-timestamp).

Going forward, render.py captures the revid atomically at fetch time; this
script only backfills articles whose sidecar is missing.

Usage:
    python pin_revids.py            # backfill all missing sidecars
    python pin_revids.py --force    # re-pin everything
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
import urllib.parse
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
ANNOT = HERE / "annotations"
OUT = HERE / "out"
_WIKILINK_RE = re.compile(
    r'href="https://en\.wikipedia\.org/wiki/([^"?]+)"[^>]*>view on Wikipedia')
WIKI_API = "https://en.wikipedia.org/w/api.php"
UA = "WikiLean/0.1 (https://github.com/Deicyde/WikiLean; jack.mccarthy.1@stonybrook.edu)"


def title_for(slug: str) -> str:
    """Recover the exact Wikipedia title. Order of preference:
      1. wikipedia_title in the final annotation JSON,
      2. the 'view on Wikipedia' permalink baked into the rendered HTML
         (recovers disambiguator parens the slug dropped, e.g.
         'Product (mathematics)'),
      3. de-slugify as a last resort."""
    jp = ANNOT / f"{slug}.json"
    if jp.exists():
        try:
            t = json.loads(jp.read_text()).get("wikipedia_title")
            if t:
                return t
        except (json.JSONDecodeError, OSError):
            pass
    hp = OUT / f"{slug}.html"
    if hp.exists():
        m = _WIKILINK_RE.search(hp.read_text())
        if m:
            return urllib.parse.unquote(m.group(1)).replace("_", " ")
    return slug.replace("_", " ")


def revision_as_of(session: requests.Session, title: str, when_iso: str) -> dict | None:
    """The latest revision at or before `when_iso` (ISO-8601 UTC)."""
    params = {
        "action": "query", "titles": title, "prop": "revisions",
        "rvprop": "ids|timestamp", "rvstart": when_iso, "rvlimit": "1",
        "rvdir": "older", "format": "json", "formatversion": "2", "redirects": "1",
    }
    for _ in range(4):
        r = session.get(WIKI_API, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", [])
        if pages and pages[0].get("revisions"):
            rev = pages[0]["revisions"][0]
            return {"revid": rev["revid"], "revision_timestamp": rev["timestamp"]}
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    htmls = sorted(CACHE.glob("*.html"))
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    n_pinned = n_skip = n_fail = 0
    for html in htmls:
        slug = html.stem
        meta_path = CACHE / f"{slug}.meta.json"
        if meta_path.exists() and not args.force:
            n_skip += 1
            continue
        mtime = dt.datetime.fromtimestamp(html.stat().st_mtime, tz=dt.timezone.utc)
        when_iso = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
        title = title_for(slug)
        rev = revision_as_of(session, title, when_iso)
        if not rev:
            n_fail += 1
            print(f"  FAIL {slug!r} (title={title!r})")
            continue
        meta = {
            "slug": slug,
            "wikipedia_title": title,
            "revid": rev["revid"],
            "revision_timestamp": rev["revision_timestamp"],
            "fetched_at": when_iso,
            "pinned_via": "mtime-backfill",
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        n_pinned += 1
        if n_pinned % 25 == 0:
            print(f"  pinned {n_pinned} …", flush=True)
        time.sleep(0.1)  # be polite to the API

    print(f"done — pinned {n_pinned}, skipped {n_skip} (already had sidecar), "
          f"failed {n_fail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
