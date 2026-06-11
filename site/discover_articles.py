#!/usr/bin/env python3
"""Discover WikiProject Mathematics articles WikiLean doesn't know about yet.

Enumerates the CURRENT article list the same way catalog/fetch_catalog.py
does — list=embeddedin over talk pages transcluding
Template:WikiProject_Mathematics (namespace 1) — reusing its HTTP layer
(api_get: maxlag=5, UA, 429/5xx retry). Unlike fetch_catalog this never
caches the enumeration: the point is the live list, not a frozen snapshot.

Each candidate article title is diffed against:
  1. live D1 slugs — one read-only `wrangler d1 execute wikilean --remote
     --json --command "SELECT slug FROM articles"`, run from wiki/ so wrangler
     picks up wrangler.jsonc; and
  2. every "title" appearing in catalog/data/*.jsonl (the frozen snapshots).

Output: site/data/new-titles.jsonl — one {"title", "slug", "source"} line per
genuinely-new article, plus a printed count. Nothing else is written anywhere.
This feeds `moderate.py new` next wave.

Usage:
    python discover_articles.py                  # full enumeration (~70 API calls)
    python discover_articles.py --max-batches 1  # one API page (testing the diff)
    python discover_articles.py --no-d1          # skip the wrangler read (offline)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_PATH = HERE / "data" / "new-titles.jsonl"

# Reuse fetch_catalog's HTTP etiquette (UA, maxlag, retry) + constants.
sys.path.insert(0, str(ROOT / "catalog"))
import fetch_catalog as fc  # noqa: E402


def make_slug(title: str) -> str:
    """Filesystem-safe slug. 'Picard–Lindelöf theorem' → 'Picard-Lindelof_theorem'.

    MUST stay byte-identical to batch_annotate.make_slug — D1 slugs were
    created with it, and the diff below compares against them. (Not imported:
    batch_annotate pulls in claude_agent_sdk at module level.)"""
    s = title.replace("–", "-").replace("—", "-")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_.\-]", "", s)
    return s


# ---------------------------------------------------------------------------
# Source 1: current WikiProject Mathematics talk-page enumeration (no cache).
# ---------------------------------------------------------------------------

def enumerate_current_titles(s, max_batches: int | None) -> tuple[list[str], bool]:
    """Return ([talk titles...], complete) — complete=False when --max-batches
    stopped the continue-paging early (a partial, testing-only view)."""
    base = {
        "action": "query",
        "list": "embeddedin",
        "eititle": fc.TEMPLATE,
        "einamespace": "1",
        "eilimit": str(fc.EI_LIMIT),
        "format": "json",
        "formatversion": "2",
    }
    cont: dict = {}
    titles: list[str] = []
    batches = 0
    t0 = time.time()
    while True:
        data = fc.api_get(s, {**base, **cont})
        for p in data.get("query", {}).get("embeddedin", []):
            titles.append(p["title"])
        batches += 1
        cont = data.get("continue", {})
        if not cont:
            return titles, True
        if max_batches is not None and batches >= max_batches:
            print(f"  enumerate: stopped at --max-batches {max_batches} "
                  f"(PARTIAL list: {len(titles)} pages)")
            return titles, False
        if batches % 10 == 0:
            print(f"  enumerate: batch {batches}, total {len(titles)} "
                  f"({time.time() - t0:.1f}s)", flush=True)


# ---------------------------------------------------------------------------
# Source 2: what WikiLean already knows (live D1 + catalog snapshots).
# ---------------------------------------------------------------------------

def d1_slugs() -> set[str]:
    """One read-only remote SELECT of every article slug."""
    proc = subprocess.run(
        ["npx", "wrangler", "d1", "execute", "wikilean", "--remote", "--json",
         "--command", "SELECT slug FROM articles"],
        cwd=str(ROOT / "wiki"), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"wrangler d1 execute failed:\n{proc.stderr[-2000:]}")
    out = proc.stdout
    # Tolerate any banner noise before the JSON payload.
    start = min((i for i in (out.find("["), out.find("{")) if i != -1), default=-1)
    if start == -1:
        raise RuntimeError("no JSON in wrangler output")
    parsed = json.loads(out[start:])
    results = (parsed[0] if isinstance(parsed, list) else parsed)["results"]
    return {r["slug"] for r in results}


def catalog_known() -> tuple[set[str], set[str]]:
    """(titles, slugs) from every catalog/data/*.jsonl record with a "title"."""
    titles: set[str] = set()
    for path in sorted((ROOT / "catalog" / "data").glob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("title") if isinstance(rec, dict) else None
            if isinstance(t, str) and t:
                titles.add(t)
    return titles, {make_slug(t) for t in titles}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Diff the live WikiProject Mathematics article list "
                    "against D1 + catalog snapshots.")
    ap.add_argument("--max-batches", type=int, default=None,
                    help="stop the enumeration after N API pages "
                         f"({fc.EI_LIMIT}/page; testing only — partial list)")
    ap.add_argument("--no-d1", action="store_true",
                    help="skip the live D1 read (offline testing; diff is "
                         "catalog-only and will OVER-report new titles)")
    ap.add_argument("--out", default=str(OUT_PATH),
                    help=f"output JSONL (default {OUT_PATH})")
    args = ap.parse_args()

    print("[1/3] enumerating current WP Math talk pages (live, uncached)")
    s = fc.make_session()
    talk_titles, complete = enumerate_current_titles(s, args.max_batches)
    print(f"  {len(talk_titles)} talk pages ({'complete' if complete else 'PARTIAL'})")

    print("[2/3] loading what WikiLean already knows")
    known_slugs: set[str] = set()
    if args.no_d1:
        print("  --no-d1: skipping live D1 slugs")
    else:
        known_slugs = d1_slugs()
        print(f"  D1 articles: {len(known_slugs)} slugs")
    cat_titles, cat_slugs = catalog_known()
    print(f"  catalog/data/*.jsonl: {len(cat_titles)} known titles")

    print("[3/3] diffing")
    new: list[dict] = []
    seen: set[str] = set()
    n_in_d1 = n_in_catalog = 0
    for talk in talk_titles:
        title = talk.removeprefix("Talk:")
        slug = make_slug(title)
        if not slug or slug in seen:
            continue
        if slug in known_slugs:
            n_in_d1 += 1
            continue
        if title in cat_titles or slug in cat_slugs:
            n_in_catalog += 1
            continue
        seen.add(slug)
        new.append({"title": title, "slug": slug, "source": "wpmath-embeddedin"})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in new:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\ncandidates scanned : {len(talk_titles)}"
          f"{' (PARTIAL — not the full project list)' if not complete else ''}")
    print(f"already in D1      : {n_in_d1}")
    print(f"in catalog only    : {n_in_catalog}")
    print(f"genuinely new      : {len(new)}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
