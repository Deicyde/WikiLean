"""Shared plumbing for brain/ingest/<db>.py external-source adapters.

Contract (brain/SCHEMA.md "External-source ingest contract"):
  catalog/data/external/<db>_pages.jsonl   {"db","id","title","url","snippet"?,"aliases"?,"qid"?,"kind_hint"?}
  catalog/data/external/<db>_links.jsonl   {"db","src","dst","context"}
First line of each file is {"_meta": {...}}. Writes are atomic (tmp+rename) and
fail-soft: raise before writing rather than truncating a previous good file.

Adapters must be deterministic (no LLM), honor source rate limits, and set `qid`
only from CC0 Wikidata property values — never guessed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXTERNAL_DIR = REPO / "catalog" / "data" / "external"
CACHE_DIR = REPO / "catalog" / ".cache" / "external"
CROSSREFS = REPO / "catalog" / "data" / "wikidata_crossrefs.json"

USER_AGENT = "WikiLean-brain/2.0 (https://wikilean.jackmccarthy.org; contact via GitHub Deicyde/WikiLean)"

# Sources whose licenses permit storing short snippets (SCHEMA.md ext payload rules).
SNIPPET_OK = {"nlab", "stacks", "lmfdb_knowl", "proofwiki", "planetmath", "oeis"}
SNIPPET_LICENSE = {
    "nlab": "nLab (attribution, no formal license)",
    "stacks": "GFDL (Stacks Project)",
    "lmfdb_knowl": "CC-BY-SA-4.0 (LMFDB)",
    "proofwiki": "CC-BY-SA-3.0 (ProofWiki)",
    "planetmath": "CC-BY-SA (PlanetMath)",
    "oeis": "CC-BY-SA-4.0 (OEIS)",
}
SNIPPET_MAX = 600  # chars, hard cap after cleanup

_WS = re.compile(r"\s+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_snippet(text: str, limit: int = SNIPPET_MAX) -> str:
    """Whitespace-normalize and hard-cap a snippet; keep inline $TeX$ as-is."""
    text = _WS.sub(" ", text or "").strip()
    if len(text) > limit:
        cut = text[:limit]
        # cut at a sentence or word boundary when possible; spaceless text
        # (URLs, data: blobs) must still land under the cap
        dot = cut.rfind(". ")
        if dot > limit // 2:
            text = cut[: dot + 1]
        else:
            head = cut.rsplit(" ", 1)[0] if " " in cut else cut[: limit - 1]
            text = head[: limit - 1] + "…"
    return text


def fetch(url: str, *, timeout: int = 60, delay: float = 0.0, retries: int = 3) -> bytes:
    """Polite GET with our UA. `delay` sleeps BEFORE the request (rate limiting)."""
    last: Exception | None = None
    for attempt in range(retries):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 — retry then re-raise
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {retries} tries: {url}: {last}")


def curl_fetch(url: str, *, timeout: int = 120) -> bytes:
    """curl fallback (the system Python SSL trust store is broken on this machine
    for some hosts — same workaround as catalog/mathlib_deps/fetch_crossrefs.py)."""
    out = subprocess.run(
        ["curl", "-sfL", "--max-time", str(timeout), "-A", USER_AGENT, url],
        capture_output=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"curl failed ({out.returncode}): {url}: {out.stderr[:200]!r}")
    return out.stdout


def qid_map(db_key: str) -> dict[str, str]:
    """external-id -> QID from catalog/data/wikidata_crossrefs.json (CC0 seeds).

    db_key is the crossrefs xref key (e.g. 'nlab', 'mathworld', 'lmfdb_knowl').
    Multi-valued xrefs map each id; on collision the first (lowest QID) wins.
    """
    data = json.loads(CROSSREFS.read_text())
    out: dict[str, str] = {}
    for qid in sorted(data.get("xrefs", {}), key=lambda q: (len(q), q)):
        for ext_id in data["xrefs"][qid].get(db_key, []):
            out.setdefault(str(ext_id), qid)
    return out


def write_jsonl(path: Path, meta: dict, rows: list[dict]) -> None:
    """Atomic jsonl write, first line _meta. Refuses to clobber with an empty set."""
    if not rows:
        raise RuntimeError(f"refusing to write 0 rows to {path} (fail-soft)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        f.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.rename(path)


def emit(db: str, pages: list[dict], links: list[dict], extra_meta: dict | None = None) -> None:
    """Validate rows against the contract and write both files atomically."""
    seen: set[str] = set()
    for p in pages:
        if p.get("db") != db or not p.get("id") or not p.get("title") or not p.get("url"):
            raise ValueError(f"bad page row: {json.dumps(p)[:200]}")
        if p["id"] in seen:
            raise ValueError(f"duplicate page id {p['id']!r}")
        seen.add(p["id"])
        if "snippet" in p:
            if db not in SNIPPET_OK:
                raise ValueError(f"{db} may not store snippets (license)")
            p["snippet"] = clean_snippet(p["snippet"])
            p["snippet_license"] = SNIPPET_LICENSE[db]
    page_ids = seen
    kept_links = []
    for e in links:
        if e.get("db") != db or not e.get("src") or not e.get("dst"):
            raise ValueError(f"bad link row: {json.dumps(e)[:200]}")
        if e["src"] == e["dst"]:
            continue
        e.setdefault("context", "body")
        kept_links.append(e)
    # links may reference pages we did not keep as rows (e.g. anchored-subset OEIS);
    # record how many resolve for the meta, but do not drop them — build_common
    # decides minting.
    resolved = sum(1 for e in kept_links if e["src"] in page_ids and e["dst"] in page_ids)
    meta = {
        "db": db,
        "fetched_at": now_iso(),
        "n_pages": len(pages),
        "n_links": len(kept_links),
        "n_links_resolved": resolved,
        **(extra_meta or {}),
    }
    write_jsonl(EXTERNAL_DIR / f"{db}_pages.jsonl", meta, pages)
    if kept_links:
        write_jsonl(EXTERNAL_DIR / f"{db}_links.jsonl", meta, kept_links)
    print(f"[{db}] wrote {len(pages)} pages, {len(kept_links)} links "
          f"({resolved} resolved) -> {EXTERNAL_DIR}", file=sys.stderr)


def read_pages(db: str) -> list[dict]:
    """Read back a pages file (skipping _meta) — for adapters that post-process."""
    path = EXTERNAL_DIR / f"{db}_pages.jsonl"
    rows = []
    with path.open() as f:
        for line in f:
            r = json.loads(line)
            if "_meta" not in r:
                rows.append(r)
    return rows


def cache_path(db: str, *parts: str) -> Path:
    p = CACHE_DIR / db
    for part in parts:
        p = p / part
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def stale(path: Path, max_age_hours: float) -> bool:
    """True if `path` is missing or older than max_age_hours (adapter cadence gate)."""
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age > max_age_hours * 3600
