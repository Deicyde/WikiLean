#!/usr/bin/env python3
"""
WikiLean catalog: enumerate WikiProject Mathematics articles on English
Wikipedia and write a per-article JSONL with metadata (class, importance,
Wikidata QID, raw banner snippet, ...).

Each phase writes an incremental JSONL cache under data/.cache/, so a run
interrupted by a 429 (or anything else) resumes by skipping already-fetched
pageids. To force re-fetch, delete the cache file (or pass --refresh).

Default output: data/articles.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
UA = (
    "WikiLean/0.1 (https://github.com/Deicyde/WikiLean; "
    "jack.mccarthy.1@stonybrook.edu)"
)
TEMPLATE = "Template:WikiProject_Mathematics"
PROP_BATCH = 50   # max titles/pageids per prop= call (non-bot)
EI_LIMIT = 500    # max per list=embeddedin call (non-bot)
WD_BATCH = 50     # max ids per wbgetentities call (non-bot)
HUMAN_QID = "Q5"  # Wikidata `human` — for is_human classification
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "data" / "articles.jsonl"
CACHE_DIR = HERE / "data" / ".cache"


# ---------------------------------------------------------------------------
# HTTP layer: api_get with maxlag, retry-on-429, and retry-on-5xx.
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})
    return s


def api_get(
    s: requests.Session,
    params: dict,
    base_url: str = API,
    max_retries: int = 8,
) -> dict:
    params = {**params, "maxlag": "5"}
    delay = 1.0
    last_status = None
    for attempt in range(max_retries):
        try:
            r = s.get(base_url, params=params, timeout=60)
        except requests.RequestException as e:
            print(f"  [network err: {e}; retry in {delay:.0f}s]", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        last_status = r.status_code
        if r.status_code == 429:
            wait = _retry_after(r) or 10
            print(f"  [429 rate-limited; sleep {wait}s]", flush=True)
            time.sleep(min(wait, 120))
            continue
        if r.status_code >= 500:
            print(f"  [{r.status_code}; backoff {delay:.0f}s]", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        try:
            data = r.json()
        except ValueError:
            r.raise_for_status()
            raise
        err = data.get("error") or {}
        if err.get("code") == "maxlag":
            wait = _retry_after(r) or 5
            print(f"  [maxlag exceeded; sleep {wait}s]", flush=True)
            time.sleep(min(wait, 120))
            continue
        r.raise_for_status()
        return data
    raise RuntimeError(f"API call failed after {max_retries} retries (last status {last_status})")


def _retry_after(r: requests.Response) -> int | None:
    v = r.headers.get("Retry-After")
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Phase 1: enumerate talk pages transcluding the WP Math banner.
# ---------------------------------------------------------------------------

def enumerate_talk_pages(s: requests.Session, cache: Path) -> list[dict]:
    if cache.exists():
        rows = [json.loads(line) for line in cache.open()]
        print(f"  enumerate: using cached list ({len(rows)} pages from {cache.name})")
        return rows

    out: list[dict] = []
    base = {
        "action": "query",
        "list": "embeddedin",
        "eititle": TEMPLATE,
        "einamespace": "1",
        "eilimit": str(EI_LIMIT),
        "format": "json",
        "formatversion": "2",
    }
    cont: dict = {}
    batches = 0
    t0 = time.time()
    while True:
        data = api_get(s, {**base, **cont})
        for p in data.get("query", {}).get("embeddedin", []):
            out.append({"talk_title": p["title"], "talk_pageid": p["pageid"]})
        batches += 1
        cont = data.get("continue", {})
        if not cont:
            break
        if batches % 10 == 0:
            print(f"  enumerate: batch {batches}, total {len(out)}", flush=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w", encoding="utf-8") as f:
        for row in out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        f"  enumerate: done — {len(out)} pages in "
        f"{time.time() - t0:.1f}s ({batches} batches)"
    )
    return out


# ---------------------------------------------------------------------------
# Banner / shell parsing.
# ---------------------------------------------------------------------------

# The WP Math banner has ~20 template-redirect aliases ({{wp math}},
# {{Maths rating}}, {{WPMATH}}, ...). We discover them at runtime via the API
# rather than hardcoding, since the list evolves.
CANONICAL_BANNER = "WikiProject Mathematics"

SHELL_RE = re.compile(
    r"\{\{\s*(?:WikiProject\s*Banner\s*Shell|WPBS|Banner\s*shell)\b",
    re.IGNORECASE,
)

# HTML comments inside template params, e.g. `class=Start<!-- foo -->`.
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def discover_banner_aliases(s: requests.Session) -> list[str]:
    """Return canonical banner name plus all template-redirects to it."""
    params = {
        "action": "query",
        "titles": f"Template:{CANONICAL_BANNER.replace(' ', '_')}",
        "prop": "redirects",
        "rdnamespace": "10",
        "rdlimit": "max",
        "format": "json",
        "formatversion": "2",
    }
    data = api_get(s, params)
    pages = data.get("query", {}).get("pages", [])
    aliases = [CANONICAL_BANNER]
    for page in pages:
        for r in page.get("redirects", []):
            title = r["title"]
            _, _, name = title.partition(":")
            aliases.append(name or title)
    return aliases


def build_banner_re(aliases: list[str]) -> re.Pattern:
    """Match the opening of any banner alias; spaces and underscores are interchangeable."""
    parts = []
    for a in aliases:
        escaped = re.escape(a.strip()).replace(r"\ ", r"[\s_]+")
        parts.append(escaped)
    pattern = r"\{\{\s*(?:" + "|".join(parts) + r")\s*[}|\n]"
    return re.compile(pattern, re.IGNORECASE)


def extract_balanced(wikitext: str, start_re: re.Pattern) -> str | None:
    """Return the first brace-balanced template snippet whose head matches start_re."""
    m = start_re.search(wikitext)
    if not m:
        return None
    start = m.start()
    depth = 0
    i = start
    n = len(wikitext)
    while i < n:
        if wikitext[i : i + 2] == "{{":
            depth += 1
            i += 2
        elif wikitext[i : i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start:i]
        else:
            i += 1
    return None


def strip_nested(s: str) -> str:
    """Blank out content at brace depth > 1 so top-level params parse cleanly."""
    out: list[str] = []
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        two = s[i : i + 2]
        if two == "{{":
            if depth >= 1:
                out.append("  ")
            else:
                out.append("{{")
            depth += 1
            i += 2
        elif two == "}}":
            depth -= 1
            if depth >= 1:
                out.append("  ")
            else:
                out.append("}}")
            i += 2
        else:
            ch = s[i]
            if depth <= 1:
                out.append(ch)
            else:
                out.append(" " if ch != "\n" else "\n")
            i += 1
    return "".join(out)


PARAM_RE = re.compile(r"\|\s*([A-Za-z][\w\-]*)\s*=\s*([^|}\n]*?)\s*(?=[|}\n])")


CLASS_CANONICAL = {
    s.lower(): s
    for s in (
        "FA", "FL", "A", "GA", "B", "C", "Start", "Stub", "List",
        "Category", "Disambig", "Draft", "File", "Portal", "Project",
        "Template", "Book", "Redirect", "NA", "Bplus", "Future",
        "Current", "Unassessed",
    )
}
IMPORTANCE_CANONICAL = {
    s.lower(): s
    for s in ("Top", "High", "Mid", "Low", "Bottom", "NA", "Unknown")
}


def normalize(v: str | None, table: dict[str, str]) -> str | None:
    if not v:
        return None
    s = v.strip()
    return table.get(s.lower(), s) or None


def parse_banner(snippet: str) -> dict[str, str]:
    snippet = COMMENT_RE.sub("", snippet)
    stripped = strip_nested(snippet)
    params: dict[str, str] = {}
    for m in PARAM_RE.finditer(stripped):
        params[m.group(1).lower()] = m.group(2).strip()
    return params


# ---------------------------------------------------------------------------
# Phase 2: per talk page, fetch wikitext and extract the WP Math banner +
# the surrounding banner-shell. Cache the extracted snippets (not the raw
# wikitext) incrementally so a 429 mid-run is cheap to recover from.
# ---------------------------------------------------------------------------

def fetch_banner_extracts(
    s: requests.Session,
    talk_pages: list[dict],
    cache: Path,
    banner_re: re.Pattern,
) -> dict[int, dict]:
    cached = _load_jsonl_dict(cache, "talk_pageid")
    if cached:
        print(f"  banner extract: resumed from {len(cached)} cached entries")
    pending = [tp for tp in talk_pages if tp["talk_pageid"] not in cached]
    if not pending:
        print(f"  banner extract: all {len(talk_pages)} entries cached")
        return cached

    cache.parent.mkdir(parents=True, exist_ok=True)
    n_done = len(cached)
    t0 = time.time()
    with cache.open("a", encoding="utf-8") as f:
        for chunk in chunks(pending, PROP_BATCH):
            params = {
                "action": "query",
                "pageids": "|".join(str(p["talk_pageid"]) for p in chunk),
                "prop": "revisions",
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "format": "json",
                "formatversion": "2",
            }
            data = api_get(s, params)
            for page in data.get("query", {}).get("pages", []):
                pid = page["pageid"]
                revs = page.get("revisions") or []
                if not revs:
                    rec = {
                        "talk_pageid": pid,
                        "rev_id": None,
                        "rev_timestamp": None,
                        "banner": None,
                        "shell": None,
                    }
                else:
                    slot = revs[0].get("slots", {}).get("main", {})
                    wt = slot.get("content") or ""
                    rec = {
                        "talk_pageid": pid,
                        "rev_id": revs[0].get("revid"),
                        "rev_timestamp": revs[0].get("timestamp"),
                        "banner": extract_balanced(wt, banner_re),
                        "shell": extract_balanced(wt, SHELL_RE),
                    }
                cached[pid] = rec
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            n_done += len(chunk)
            if n_done % 1000 < PROP_BATCH:
                print(
                    f"  banner extract: {n_done}/{len(talk_pages)}  "
                    f"({time.time() - t0:.1f}s)",
                    flush=True,
                )
    print(f"  banner extract: done — {len(cached)} pages in {time.time() - t0:.1f}s")
    return cached


# ---------------------------------------------------------------------------
# Phase 3: resolve article titles to pageid + Wikidata QID. Same incremental
# caching pattern.
# ---------------------------------------------------------------------------

def fetch_article_meta(
    s: requests.Session, titles: list[str], cache: Path
) -> dict[str, dict]:
    cached = _load_jsonl_dict(cache, "title")
    if cached:
        print(f"  article meta: resumed from {len(cached)} cached entries")
    pending = [t for t in titles if t not in cached]
    if not pending:
        print(f"  article meta: all {len(titles)} entries cached")
        return cached

    cache.parent.mkdir(parents=True, exist_ok=True)
    n_done = len(cached)
    t0 = time.time()
    with cache.open("a", encoding="utf-8") as f:
        for chunk in chunks(pending, PROP_BATCH):
            params = {
                "action": "query",
                "titles": "|".join(chunk),
                "prop": "pageprops|info",
                "ppprop": "wikibase_item",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
            }
            data = api_get(s, params)
            q = data.get("query", {})
            norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
            redir = {r["from"]: r["to"] for r in q.get("redirects", [])}
            by_resolved: dict[str, dict] = {}
            for page in q.get("pages", []):
                title = page.get("title")
                if page.get("missing"):
                    by_resolved[title] = {
                        "pageid": None,
                        "wikidata_qid": None,
                        "missing": True,
                    }
                else:
                    by_resolved[title] = {
                        "pageid": page.get("pageid"),
                        "wikidata_qid": (page.get("pageprops") or {}).get(
                            "wikibase_item"
                        ),
                        "missing": False,
                    }
            for t in chunk:
                cur = norm.get(t, t)
                cur = redir.get(cur, cur)
                info = by_resolved.get(
                    cur,
                    {"pageid": None, "wikidata_qid": None, "missing": True},
                )
                rec = {"title": t, "resolved_title": cur, **info}
                cached[t] = rec
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            n_done += len(chunk)
            if n_done % 1000 < PROP_BATCH:
                print(
                    f"  article meta: {n_done}/{len(titles)}  "
                    f"({time.time() - t0:.1f}s)",
                    flush=True,
                )
    print(f"  article meta: done — {len(cached)} titles in {time.time() - t0:.1f}s")
    return cached


# ---------------------------------------------------------------------------
# Phase 4: for each Wikidata QID, fetch P31 (instance of) to distinguish
# biographies (Q5) from mathematical concepts. Hits wikidata.org, not enwiki.
# ---------------------------------------------------------------------------

def fetch_wikidata_p31(
    s: requests.Session, qids: list[str], cache: Path
) -> dict[str, dict]:
    cached = _load_jsonl_dict(cache, "qid")
    if cached:
        print(f"  wikidata P31: resumed from {len(cached)} cached entries")
    pending = [q for q in qids if q and q not in cached]
    if not pending:
        print(f"  wikidata P31: all {sum(1 for q in qids if q)} qids cached")
        return cached

    cache.parent.mkdir(parents=True, exist_ok=True)
    n_done = len(cached)
    t0 = time.time()
    with cache.open("a", encoding="utf-8") as f:
        for chunk in chunks(pending, WD_BATCH):
            params = {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": "claims",
                "format": "json",
                "formatversion": "2",
            }
            data = api_get(s, params, base_url=WIKIDATA_API)
            entities = data.get("entities", {})
            for qid in chunk:
                ent = entities.get(qid, {})
                if ent.get("missing") is not None and "claims" not in ent:
                    rec = {"qid": qid, "p31": [], "missing": True}
                else:
                    p31_claims = (ent.get("claims") or {}).get("P31", [])
                    p31: list[str] = []
                    for claim in p31_claims:
                        val = (
                            (claim.get("mainsnak") or {})
                            .get("datavalue", {})
                            .get("value", {})
                        )
                        qv = val.get("id") if isinstance(val, dict) else None
                        if qv:
                            p31.append(qv)
                    rec = {"qid": qid, "p31": p31, "missing": False}
                cached[qid] = rec
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            n_done += len(chunk)
            if n_done % 1000 < WD_BATCH:
                print(
                    f"  wikidata P31: {n_done}/{len(qids)}  "
                    f"({time.time() - t0:.1f}s)",
                    flush=True,
                )
    print(f"  wikidata P31: done — {len(cached)} qids in {time.time() - t0:.1f}s")
    return cached


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _load_jsonl_dict(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    out = {}
    for line in path.open():
        rec = json.loads(line)
        out[rec[key]] = rec
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--limit", type=int, default=None, help="Cap talk-page count (for testing)"
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Delete cache files before running (full re-fetch)",
    )
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_pages = CACHE_DIR / "talk_pages.jsonl"
    cache_banner = CACHE_DIR / "banner_extract.jsonl"
    cache_meta = CACHE_DIR / "article_meta.jsonl"
    cache_p31 = CACHE_DIR / "wikidata_p31.jsonl"
    if args.refresh:
        for p in (cache_pages, cache_banner, cache_meta, cache_p31):
            p.unlink(missing_ok=True)
        print("  --refresh: removed cache files")

    s = make_session()

    print("[1/4] enumerating talk pages transcluding the banner")
    talk_pages = enumerate_talk_pages(s, cache_pages)
    if args.limit:
        talk_pages = talk_pages[: args.limit]
        print(f"  --limit applied: keeping {len(talk_pages)} pages")

    print("  discovering banner aliases via API")
    aliases = discover_banner_aliases(s)
    banner_re = build_banner_re(aliases)
    print(f"  using {len(aliases)} banner alias(es): {', '.join(aliases[:6])}{', ...' if len(aliases) > 6 else ''}")

    print("[2/4] fetching talk-page wikitext and extracting banner / shell")
    extracts = fetch_banner_extracts(s, talk_pages, cache_banner, banner_re)

    article_titles = [tp["talk_title"].removeprefix("Talk:") for tp in talk_pages]
    print("[3/4] fetching article metadata (pageid, Wikidata QID)")
    article_meta = fetch_article_meta(s, article_titles, cache_meta)

    in_scope = set(article_titles)
    qids = sorted({
        m["wikidata_qid"]
        for t, m in article_meta.items()
        if t in in_scope and m.get("wikidata_qid")
    })
    print(f"[4/4] fetching Wikidata P31 (instance of) for {len(qids)} unique QIDs")
    p31_records = fetch_wikidata_p31(s, qids, cache_p31)

    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n_with_class = n_with_imp = n_with_qid = n_human = 0
    n_missing = n_no_banner = 0

    print(f"\nwriting {out_path}")
    with out_path.open("w", encoding="utf-8") as f:
        for tp in talk_pages:
            talk_title = tp["talk_title"]
            article_title = talk_title.removeprefix("Talk:")
            ext = extracts.get(tp["talk_pageid"]) or {}
            banner = ext.get("banner")
            shell = ext.get("shell")
            banner_params = parse_banner(banner) if banner else {}
            shell_params = parse_banner(shell) if shell else {}
            meta = article_meta.get(article_title, {})

            cls = normalize(
                shell_params.get("class") or banner_params.get("class"),
                CLASS_CANONICAL,
            )
            imp = normalize(
                banner_params.get("importance") or banner_params.get("priority"),
                IMPORTANCE_CANONICAL,
            )
            historical_raw = banner_params.get("historical")
            historical = (
                historical_raw.lower() in ("yes", "1", "true")
                if historical_raw
                else None
            )

            qid = meta.get("wikidata_qid")
            p31 = (p31_records.get(qid) or {}).get("p31") if qid else None
            is_human = (HUMAN_QID in p31) if p31 else False

            if cls:
                n_with_class += 1
            if imp:
                n_with_imp += 1
            if qid:
                n_with_qid += 1
            if is_human:
                n_human += 1
            if meta.get("missing"):
                n_missing += 1
            if not banner:
                n_no_banner += 1

            rec = {
                "title": article_title,
                "talk_title": talk_title,
                "pageid": meta.get("pageid"),
                "talk_pageid": tp["talk_pageid"],
                "wikidata_qid": qid,
                "p31": p31,
                "is_human": is_human,
                "class": cls,
                "importance": imp,
                "field": banner_params.get("field") or None,
                "historical": historical,
                "talk_rev_id": ext.get("rev_id"),
                "talk_rev_timestamp": ext.get("rev_timestamp"),
                "raw_banner": banner,
                "raw_shell": shell,
                "article_missing": meta.get("missing", False),
                "fetched_at": fetched_at,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = len(talk_pages) or 1
    print("\nstats:")
    print(f"  total articles:      {len(talk_pages)}")
    print(f"  with class:          {n_with_class}  ({n_with_class / total * 100:.1f}%)")
    print(f"  with importance:     {n_with_imp}  ({n_with_imp / total * 100:.1f}%)")
    print(f"  with Wikidata QID:   {n_with_qid}  ({n_with_qid / total * 100:.1f}%)")
    print(f"  is_human (Q5 in P31):{n_human}  ({n_human / total * 100:.1f}%)")
    print(f"  no banner extracted: {n_no_banner}")
    print(f"  article missing:     {n_missing}  (talk exists, mainspace doesn't)")
    size = out_path.stat().st_size
    print(f"\nwrote {out_path}  ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
