#!/usr/bin/env python3
"""Encyclopedia of Mathematics ingest (links+titles, NO snippets — Springer
copyright bars storing article text; ids/titles/links only, display deep-links).

Open MediaWiki API walk: generator=allpages (ns 0) with prop=links, following
the 'continue' chain fully (~16k pages). Page ids use underscores to match the
P7554 identifier format in wikidata_crossrefs.json. curl_fetch throughout (the
system Python SSL trust store is broken for some hosts on this machine).

Run: python3 brain/ingest/eom.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

API = "https://encyclopediaofmath.org/api.php"
WIKI = "https://encyclopediaofmath.org/wiki/"
DELAY = 0.5


def page_id(title: str) -> str:
    return title.replace(" ", "_")


def main() -> int:
    params = {
        "action": "query", "format": "json", "generator": "allpages",
        "gaplimit": "500", "gapnamespace": "0",
        "prop": "links", "plnamespace": "0", "pllimit": "max",
    }
    titles: dict[str, str] = {}          # page_id -> display title
    out_links: dict[str, set[str]] = {}  # page_id -> set(dst page_id)
    cont: dict[str, str] = {}
    n_calls = 0
    while True:
        time.sleep(DELAY)
        url = API + "?" + urllib.parse.urlencode({**params, **cont})
        data = json.loads(common.curl_fetch(url))
        n_calls += 1
        cache = common.cache_path("eom", f"allpages_{n_calls:04d}.json")
        cache.write_text(json.dumps(data, ensure_ascii=False))
        for page in data.get("query", {}).get("pages", {}).values():
            if page.get("ns") != 0:
                continue
            pid = page_id(page["title"])
            titles.setdefault(pid, page["title"])
            dsts = out_links.setdefault(pid, set())
            for link in page.get("links", []):
                if link.get("ns") == 0:
                    dsts.add(page_id(link["title"]))
        cont = data.get("continue") or {}
        if not cont:
            break
        if n_calls % 25 == 0:
            print(f"[eom] {n_calls} API calls, {len(titles)} pages so far",
                  file=sys.stderr)

    qids = common.qid_map("eom")
    pages = []
    for pid in sorted(titles):
        row = {"db": "eom", "id": pid, "title": titles[pid],
               "url": WIKI + urllib.parse.quote(pid, safe="()_,'-./:")}
        if pid in qids:
            row["qid"] = qids[pid]
        pages.append(row)
    link_rows = [{"db": "eom", "src": src, "dst": dst, "context": "body"}
                 for src in sorted(out_links) for dst in sorted(out_links[src])
                 if dst != src]

    common.emit("eom", pages, link_rows, extra_meta={
        "source_pin": f"encyclopediaofmath.org api.php allpages walk ({n_calls} calls)",
        "n_api_calls": n_calls,
        "n_with_qid": sum(1 for p in pages if "qid" in p),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
