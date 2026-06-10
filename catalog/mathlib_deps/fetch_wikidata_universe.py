#!/usr/bin/env python3
"""Pull a Wikidata-centric universe of math QIDs.

For each math class in CLASSES, queries WDQS for entities whose P31 is that class
or one of its subclasses (transitively via P279*). Collects qid + English label
+ enwiki article slug (if any).

Per-class queries so one timeout doesn't kill the whole run. Output:
  catalog/data/wikidata_universe.jsonl
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "wikidata_universe.jsonl"

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "WikiLean/0.1 (https://wikilean.jackmccarthy.org)"
PAUSE = 2.0

# Math classes verified against canonical entities + the existing concept-layer
# P31 distribution. Direct P31 only — no subclass closure (each class is small
# enough that closure isn't needed and would risk WDQS timeouts).
CLASSES: dict[str, str] = {
    "Q65943":    "theorem",
    "Q319141":   "conjecture",
    "Q207505":   "lemma",
    "Q11538":    "mathematical proof",
    "Q1166625":  "mathematical problem",
    "Q24034552": "mathematical concept",
    "Q20026918": "mathematical theory",
    "Q1936384":  "branch of mathematics",
    "Q976981":   "formula",
    "Q6498784":  "mathematical expression",
    "Q186509":   "mathematical constant",
    "Q21550639": "geometric concept",
}


def query(sparql: str, timeout: int = 180) -> dict:
    data = urllib.parse.urlencode({"query": sparql}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read(), strict=False)


def main() -> None:
    entities: dict[str, dict] = {}
    failed: list[str] = []

    for cls_qid, cls_name in CLASSES.items():
        sparql = f"""
SELECT ?x ?xLabel ?article WHERE {{
  ?x wdt:P31 wd:{cls_qid} .
  OPTIONAL {{
    ?article schema:about ?x ;
             schema:isPartOf <https://en.wikipedia.org/> .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""
        print(f"fetching {cls_name} ({cls_qid})...", flush=True)
        try:
            res = query(sparql)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            failed.append(cls_qid)
            continue

        n = 0
        for b in res["results"]["bindings"]:
            ent_qid = b["x"]["value"].rsplit("/", 1)[-1]
            label = b.get("xLabel", {}).get("value", ent_qid)
            slug = None
            if "article" in b:
                url = b["article"]["value"]
                slug = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            ent = entities.setdefault(
                ent_qid,
                {"qid": ent_qid, "label": label, "classes": [], "enwiki_slug": slug},
            )
            ent["classes"].append(cls_qid)
            if slug and not ent["enwiki_slug"]:
                ent["enwiki_slug"] = slug
            n += 1
        print(f"  -> {n} bindings; {len(entities)} unique entities so far", flush=True)
        time.sleep(PAUSE)

    with OUT.open("w") as f:
        for e in entities.values():
            e["classes"] = sorted(set(e["classes"]))
            f.write(json.dumps(e) + "\n")

    print(f"\nwrote {len(entities)} entities -> {OUT}")
    with_enwiki = sum(1 for e in entities.values() if e["enwiki_slug"])
    print(f"  with enwiki article: {with_enwiki}")
    print(f"  wikidata-only:        {len(entities) - with_enwiki}")
    if failed:
        print(f"WARN: {len(failed)} class queries failed: {failed}")


if __name__ == "__main__":
    main()
