#!/usr/bin/env python3
"""Backfill wikidata_universe rows for QIDs the SPARQL class sweep missed.

The universe (catalog/data/wikidata_universe.jsonl) is built per-P31-class, so
graph nodes whose P31 falls outside CLASSES (or is absent) never get a row.
This script wbgetentities-batches an explicit QID list (50/call, the anon max)
for en label + en description + P31 classes + enwiki sitelink and writes
catalog/data/universe_extension.jsonl rows
  {qid, label, description, classes, enwiki_slug, source: "graph_v2_gap"}.
Redirected QIDs keep the requested qid and gain "redirect_to"; deleted QIDs get
no row and are reported. Deterministic (no LLM). Atomic write.

Run: python3 catalog/fetch_universe_extension.py <qids.json>
where <qids.json> is a JSON array of "Q..." strings or of objects with a "qid".
"""
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
OUT = DATA / "universe_extension.jsonl"
API = "https://www.wikidata.org/w/api.php"
UA = "WikiLean/1.0 (https://wikilean.jackmccarthy.org)"
BATCH = 50
RETRIES = 3


def wbgetentities(qids: list[str]) -> dict[str, dict]:
    # curl, not urllib: the system Python's SSL trust store is broken on this
    # machine (same reason fetch_crossrefs.py / bot/poll.py shell out to curl).
    url = API + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(qids), "format": "json",
        "props": "labels|descriptions|claims|sitelinks",
        "languages": "en", "sitefilter": "enwiki", "maxlag": "5"})
    for attempt in range(RETRIES):
        try:
            out = subprocess.run(
                ["curl", "-sS", "-m", "90", "--retry", "2",
                 "-H", f"User-Agent: {UA}", url],
                capture_output=True, text=True, timeout=120, check=True).stdout
            resp = json.loads(out)
            if "error" in resp:  # maxlag / throttle — back off, retry
                raise RuntimeError(resp["error"].get("code", "api-error"))
            return resp["entities"]
        except Exception as e:  # noqa: BLE001 — retry then re-raise
            if attempt == RETRIES - 1:
                raise
            print(f"  retry {attempt + 1} after {e}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    return {}


def to_row(qid: str, ent: dict) -> dict:
    classes = sorted({
        c["mainsnak"]["datavalue"]["value"]["id"]
        for c in ent.get("claims", {}).get("P31", [])
        if c["mainsnak"].get("snaktype") == "value"})
    slug = ent.get("sitelinks", {}).get("enwiki", {}).get("title")
    row = {"qid": qid,
           "label": ent.get("labels", {}).get("en", {}).get("value"),
           "description": ent.get("descriptions", {}).get("en", {}).get("value"),
           "classes": classes,
           "enwiki_slug": slug.replace(" ", "_") if slug else None,
           "source": "graph_v2_gap"}
    redirect = ent.get("redirects", {}).get("to") or (
        ent["id"] if ent.get("id") not in (None, qid) else None)
    if redirect:
        row["redirect_to"] = redirect
    return row


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    raw = json.loads(Path(sys.argv[1]).read_text())
    qids = sorted({r if isinstance(r, str) else r["qid"] for r in raw})
    print(f"{len(qids)} QIDs → wbgetentities in batches of {BATCH}")
    rows: list[dict] = []
    missing: list[str] = []
    for i in range(0, len(qids), BATCH):
        chunk = qids[i:i + BATCH]
        entities = wbgetentities(chunk)
        # A redirected request may come back keyed by the TARGET id with a
        # redirects.from field — index both so every requested qid resolves.
        by_from = {e["redirects"]["from"]: e for e in entities.values()
                   if "redirects" in e}
        for qid in chunk:
            ent = entities.get(qid) or by_from.get(qid)
            if ent is None or "missing" in ent:
                missing.append(qid)
                continue
            rows.append(to_row(qid, ent))
        print(f"  {min(i + BATCH, len(qids))}/{len(qids)} … "
              f"{len(rows)} rows, {len(missing)} missing")
        time.sleep(1)  # polite to the API
    tmp = OUT.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(OUT)  # atomic — a failed run never truncates the live file
    redirects = [(r["qid"], r["redirect_to"]) for r in rows if "redirect_to" in r]
    branch = sum(1 for r in rows if "Q1936384" in r["classes"])
    print(f"wrote {OUT.name}: {len(rows)} rows "
          f"({branch} with P31=Q1936384 branch-of-mathematics, "
          f"{len(redirects)} redirected, {len(missing)} deleted/missing)")
    for qid, to in redirects:
        print(f"  redirect: {qid} → {to}")
    for qid in missing:
        print(f"  missing: {qid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
