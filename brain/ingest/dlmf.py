#!/usr/bin/env python3
"""DLMF ingest (ids+titles+links, NO snippets — NIST bars redistribution of
content; section ids, titles and cross-reference hrefs only, display deep-links).

Polite crawl (1 req/s, cached aggressively): the idx page + chapter TOCs 1..36
enumerate ~600 numeric sections; each section page is fetched ONLY to extract
its <title> and href cross-references (./25.2#E1 style -> dst section 25.2).
At most DLMF_MAX_FETCH (default 700) uncached pages per run — cached pages make
later runs nearly free. qid_map('dlmf') values are equation/subsection-level
(e.g. '1.2.E34', '1.10.v'); they are normalized to their section id.

Run: python3 brain/ingest/dlmf.py
"""
from __future__ import annotations

import html
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

BASE = "https://dlmf.nist.gov/"
N_CHAPTERS = 36
DELAY = 1.0
HREF = re.compile(r'href="([^"]+)"')
SECTION_HREF = re.compile(r"^(?:\./|\.\./|/)?(\d+\.\d+)(?:#|$)")
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
SECTION_OF = re.compile(r"^(\d+)\.\d+")


def cached_fetch(name: str, url: str, budget: list[int]) -> str | None:
    """Cache-first page fetch; uncached fetches spend budget[0] and sleep 1s.

    Cache writes are atomic (tmp+rename) and a zero-byte cached file is
    treated as absent — cached files are trusted unconditionally, so a killed
    or empty-response run must never poison the cache."""
    cache = common.cache_path("dlmf", f"{name}.html")
    if cache.exists() and cache.stat().st_size == 0:
        cache.unlink()
    if not cache.exists():
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        time.sleep(DELAY)
        try:
            data = common.curl_fetch(url)
            if not data:
                raise RuntimeError("empty response body")
            common.atomic_write_bytes(cache, data)
        except Exception as e:  # noqa: BLE001 — retry on a later run
            print(f"[dlmf] fetch {url} failed: {e}", file=sys.stderr)
            return None
    return cache.read_text(errors="replace")


def hrefs_to_sections(page: str) -> list[str]:
    out = []
    for h in HREF.findall(page):
        m = SECTION_HREF.match(h)
        if m:
            out.append(m.group(1))
    return out


def clean_title(page: str) -> str | None:
    m = TITLE.search(page)
    if not m:
        return None
    # "DLMF: §25.2 Definition and Expansions ‣ Riemann Zeta ‣ Chapter 25 ..."
    t = html.unescape(m.group(1)).strip()
    t = re.sub(r"^DLMF:\s*", "", t)
    return t.split("‣")[0].strip()


def section_qids() -> dict[str, str]:
    """section id -> QID; P11497 values are equation/subsection-granular."""
    out: dict[str, str] = {}
    for value, qid in sorted(common.qid_map("dlmf").items()):
        m = re.match(r"^(\d+\.\d+)", value)
        if m:
            out.setdefault(m.group(1), qid)
    return out


def main() -> int:
    budget = [int(os.environ.get("DLMF_MAX_FETCH", "700"))]

    sections: set[str] = set()
    idx = cached_fetch("idx", BASE + "idx", budget)
    if idx:
        sections.update(s for s in hrefs_to_sections(idx)
                        if 1 <= int(SECTION_OF.match(s).group(1)) <= N_CHAPTERS)
    for ch in range(1, N_CHAPTERS + 1):
        toc = cached_fetch(f"toc_{ch}", f"{BASE}{ch}", budget)
        if toc:
            # keep own-chapter sections only (prev/next nav links cross chapters)
            sections.update(s for s in hrefs_to_sections(toc)
                            if SECTION_OF.match(s).group(1) == str(ch))

    def sec_key(s: str) -> tuple[int, int]:
        a, b = s.split(".")
        return int(a), int(b)

    qids = section_qids()
    pages, links = [], set()
    pending = 0
    for sec in sorted(sections, key=sec_key):
        page = cached_fetch(f"section_{sec}", BASE + sec, budget)
        if page is None:
            pending += 1
            continue
        title = clean_title(page)
        if not title:
            pending += 1
            continue
        row = {"db": "dlmf", "id": sec, "title": title, "url": BASE + sec,
               "kind_hint": "section"}
        if sec in qids:
            row["qid"] = qids[sec]
        pages.append(row)
        for dst in hrefs_to_sections(page):
            if dst != sec and dst in sections:
                links.add((sec, dst))

    link_rows = [{"db": "dlmf", "src": s, "dst": d, "context": "body"}
                 for s, d in sorted(links)]
    common.emit("dlmf", pages, link_rows, extra_meta={
        "source_pin": "dlmf.nist.gov idx + chapter TOC crawl",
        "n_sections_enumerated": len(sections),
        "n_sections_pending": pending,
        "fetch_budget_left": budget[0],
        "n_with_qid": sum(1 for p in pages if "qid" in p),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
