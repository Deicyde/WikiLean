#!/usr/bin/env python3
"""OEIS ingest (names + anchored edges). Names come from the daily names.gz
dump (full A-number inventory, cached). Pages are minted ONLY for the anchored
subset — A-numbers carried by a Wikidata P829 crossref — and per-entry JSON is
fetched (cached forever) for those to harvest `xref` cross-references. Edges
keep only anchored→anchored pairs (context 'related'). CC-BY-SA 4.0 permits
the one-line name as snippet.

Run: python3 brain/ingest/oeis.py   (OEIS_DELAY env overrides the 1.5s delay)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

NAMES_URL = "https://oeis.org/names.gz"
ENTRY_URL = "https://oeis.org/{}?fmt=json"
PAGE_URL = "https://oeis.org/{}"
ANUM = re.compile(r"A\d{6}")
DELAY = float(os.environ.get("OEIS_DELAY", "1.5"))


def load_names() -> dict[str, str]:
    cache = common.cache_path("oeis", "names.gz")
    if common.stale(cache, 20):
        cache.write_bytes(common.curl_fetch(NAMES_URL, timeout=300))
    names: dict[str, str] = {}
    with gzip.open(cache, "rt", errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                continue
            aid, _, name = line.rstrip("\n").partition(" ")
            if ANUM.fullmatch(aid) and name:
                names[aid] = name.strip()
    return names


def entry_json(aid: str, fetched: list[int]) -> dict | None:
    cache = common.cache_path("oeis", "json", f"{aid}.json")
    if not cache.exists():
        fetched[0] += 1
        time.sleep(DELAY)
        try:
            cache.write_bytes(common.curl_fetch(ENTRY_URL.format(aid)))
        except Exception as e:  # noqa: BLE001 — retry on a later run
            print(f"[oeis] {aid} fetch failed: {e}", file=sys.stderr)
            return None
    try:
        return json.loads(cache.read_text())
    except json.JSONDecodeError:
        cache.unlink()  # poisoned cache entry; refetch next run
        return None


def main() -> int:
    names = load_names()
    qids = common.qid_map("oeis")
    anchored = sorted(a for a in qids if ANUM.fullmatch(a))

    pages, links = [], set()
    fetched = [0]
    for aid in anchored:
        entry = entry_json(aid, fetched)
        name = names.get(aid) or (entry or {}).get("name", "").strip()
        if not name:
            print(f"[oeis] {aid} has no name; skipping page", file=sys.stderr)
            continue
        pages.append({"db": "oeis", "id": aid, "title": name, "snippet": name,
                      "url": PAGE_URL.format(aid), "qid": qids[aid]})
        if entry:
            xref_text = " ".join(entry.get("xref") or [])
            for dst in ANUM.findall(xref_text):
                if dst != aid and dst in qids:
                    links.add((aid, dst))

    link_rows = [{"db": "oeis", "src": s, "dst": d, "context": "related"}
                 for s, d in sorted(links)]
    common.emit("oeis", pages, link_rows, extra_meta={
        "source_pin": "oeis.org names.gz + per-entry ?fmt=json",
        "n_names_inventory": len(names),
        "n_anchored": len(anchored),
        "n_entry_fetched_this_run": fetched[0],
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
