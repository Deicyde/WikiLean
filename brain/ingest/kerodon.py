#!/usr/bin/env python3
"""Kerodon ingest (links+metadata, NO snippets — Kerodon is unlicensed).

Two Gerby structure trees (roots 0000 and 02GZ) give the full tag inventory.
Edges come from per-tag /data/tag/<TAG>/content/full HTML, harvested via a
href="/tag/XXXX" regex. Content fetches are deliberately incremental: at most
KERODON_MAX_FETCH (default 400) uncached tags per run at a polite 1.2s delay;
every response is cached so nightly (monthly-cadence) runs accumulate coverage.
Edges are emitted only from tags whose content is already cached.

Run: python3 brain/ingest/kerodon.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

ROOTS = ("0000", "02GZ")
STRUCTURE_URL = "https://kerodon.net/data/tag/{}/structure"
CONTENT_URL = "https://kerodon.net/data/tag/{}/content/full"
PAGE_URL = "https://kerodon.net/tag/{}"
HREF = re.compile(r'href="/tag/([0-9A-Z]{4})"')
DELAY = 1.2


def load_structure(root: str) -> dict:
    """Fetch a structure tree, falling back to the cached copy on failure.
    Cache write is atomic (tmp+rename, after a successful parse); a zero-byte
    or unparseable cached copy is treated as absent."""
    cache = common.cache_path("kerodon", f"structure_{root}.json")
    try:
        raw = common.curl_fetch(STRUCTURE_URL.format(root))
        data = json.loads(raw)
        common.atomic_write_bytes(cache, raw)
        return data
    except Exception as e:  # noqa: BLE001 — fail-soft to cache
        if cache.exists() and cache.stat().st_size > 0:
            try:
                data = json.loads(cache.read_text())
            except ValueError:
                pass  # poisoned cache — surface the fetch error instead
            else:
                print(f"[kerodon] structure {root} fetch failed ({e}); using cache",
                      file=sys.stderr)
                return data
        raise


def flatten(node: dict, pages: dict[str, dict]) -> None:
    tag = node.get("tag")
    if tag and tag not in pages:
        title = node.get("name") or " ".join(
            x for x in (str(node.get("type", "")).capitalize(),
                        node.get("reference", "")) if x).strip() or tag
        row = {"db": "kerodon", "id": tag, "title": title,
               "url": PAGE_URL.format(tag)}
        if node.get("type"):
            row["kind_hint"] = node["type"]
        pages[tag] = row
    for child in node.get("children", []) or []:
        flatten(child, pages)


def main() -> int:
    pages: dict[str, dict] = {}
    for root in ROOTS:
        flatten(load_structure(root), pages)

    max_fetch = int(os.environ.get("KERODON_MAX_FETCH", "400"))
    fetched = errors = 0
    for tag in sorted(pages):
        path = common.cache_path("kerodon", "content", f"{tag}.html")
        # a zero-byte cached file (killed run / empty response) is absent
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
        if path.exists() or fetched >= max_fetch:
            continue
        fetched += 1
        time.sleep(DELAY)
        try:
            data = common.curl_fetch(CONTENT_URL.format(tag))
            if not data:
                raise RuntimeError("empty response body")
            common.atomic_write_bytes(path, data)  # never a truncated cache file
        except Exception as e:  # noqa: BLE001 — retry on a later run
            errors += 1
            print(f"[kerodon] content {tag} failed: {e}", file=sys.stderr)

    links: set[tuple[str, str]] = set()
    cached = 0
    content_dir = common.CACHE_DIR / "kerodon" / "content"
    for path in sorted(content_dir.glob("*.html")) if content_dir.exists() else []:
        src = path.stem
        if src not in pages or path.stat().st_size == 0:
            continue  # zero-byte cache = absent (poisoned by a killed run)
        cached += 1
        for dst in HREF.findall(path.read_text(errors="replace")):
            if dst != src:
                links.add((src, dst))
    link_rows = [{"db": "kerodon", "src": s, "dst": d, "context": "body"}
                 for s, d in sorted(links)]

    common.emit("kerodon", sorted(pages.values(), key=lambda p: p["id"]),
                link_rows, extra_meta={
        "source_pin": "kerodon.net Gerby API (roots 0000, 02GZ)",
        "n_content_cached": cached,
        "n_content_fetched_this_run": fetched,
        "n_fetch_errors": errors,
        "max_fetch": max_fetch,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
