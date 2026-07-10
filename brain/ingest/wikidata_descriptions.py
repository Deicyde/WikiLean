#!/usr/bin/env python3
"""Wikidata descriptions fetcher — catalog/data/wikidata_descriptions.json.

Collects every concept QID the Brain builds over (rebuild_grounding.json +
universe_extension.jsonl + wikidata_crossrefs.json), seeds descriptions already
on hand (universe_extension rows + any previous output — refetch is pointless
nightly churn), and fetches the rest via wbgetentities in batches of 50
(props=descriptions, languages=en, CC0). Output shape (SCHEMA.md v2):
{"_meta": {...}, "descriptions": {qid: text}}. Atomic write.

Run: python3 brain/ingest/wikidata_descriptions.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

API = "https://www.wikidata.org/w/api.php"
OUT = common.REPO / "catalog" / "data" / "wikidata_descriptions.json"
GROUNDING = common.REPO / "catalog" / "data" / "rebuild_grounding.json"
UNIVERSE_EXT = common.REPO / "catalog" / "data" / "universe_extension.jsonl"
BATCH = 50
DELAY = 0.3


def collect_qids() -> tuple[set[str], dict[str, str]]:
    """All concept QIDs + descriptions already present in universe_extension."""
    qids: set[str] = set()
    seed: dict[str, str] = {}
    for row in json.loads(GROUNDING.read_text()):
        if isinstance(row.get("qid"), str) and row["qid"].startswith("Q"):
            qids.add(row["qid"])
    with UNIVERSE_EXT.open() as f:
        for line in f:
            row = json.loads(line)
            qid = row.get("qid")
            if isinstance(qid, str) and qid.startswith("Q"):
                qids.add(qid)
                if row.get("description"):
                    seed[qid] = row["description"]
    qids.update(json.loads(common.CROSSREFS.read_text()).get("xrefs", {}))
    return qids, seed


def load_previous() -> dict[str, str]:
    """Previous output; tolerates the pre-v2 flat {qid: text} shape."""
    if not OUT.exists():
        return {}
    try:
        data = json.loads(OUT.read_text())
    except json.JSONDecodeError:
        return {}
    if "descriptions" in data:
        return dict(data["descriptions"])
    return {q: d for q, d in data.items()
            if q.startswith("Q") and isinstance(d, str)}


def fetch_batch(batch: list[str]) -> dict[str, str]:
    time.sleep(DELAY)
    url = API + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(batch),
        "props": "descriptions", "languages": "en", "format": "json",
    })
    data = json.loads(common.curl_fetch(url))
    out = {}
    for qid, ent in data.get("entities", {}).items():
        desc = ent.get("descriptions", {}).get("en", {}).get("value")
        if desc and qid.startswith("Q"):
            out[qid] = desc
    return out


def main() -> int:
    qids, descriptions = collect_qids()
    descriptions.update(load_previous())  # previous fetches win over seed text
    missing = sorted(q for q in qids if q not in descriptions)
    print(f"[wikidata_descriptions] {len(qids)} QIDs, {len(missing)} to fetch",
          file=sys.stderr)
    fetched = 0
    for i in range(0, len(missing), BATCH):
        batch = missing[i:i + BATCH]
        try:
            got = fetch_batch(batch)
        except Exception as e:  # noqa: BLE001 — keep what we have (fail-soft)
            print(f"[wikidata_descriptions] batch {i // BATCH} failed: {e}",
                  file=sys.stderr)
            continue
        descriptions.update(got)
        fetched += len(got)

    descriptions = {q: d for q, d in descriptions.items() if q in qids}
    if not descriptions:
        raise RuntimeError("refusing to write 0 descriptions (fail-soft)")
    payload = {
        "_meta": {
            "fetched_at": common.now_iso(),
            "source": "wikidata wbgetentities (props=descriptions, en) + universe_extension.jsonl seed",
            "n_qids": len(qids),
            "n_descriptions": len(descriptions),
            "n_fetched_this_run": fetched,
        },
        "descriptions": descriptions,
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(OUT)
    print(f"[wikidata_descriptions] wrote {len(descriptions)} descriptions -> {OUT}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
