#!/usr/bin/env python3
"""Pull the Wikidata induced subgraph over the concept-layer QIDs.

For each batch of subject QIDs, asks WDQS for all outgoing direct-claim
statements whose object is a Wikidata item, then keeps only the edges whose
object is also one of our QIDs. Filtering in Python instead of inside SPARQL
keeps the query small and predictable.

Output: wikidata_edges.jsonl, one edge per line:
  {"s": "Q...", "p": "P...", "p_label": "...", "o": "Q..."}
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
CONCEPT = HERE.parent / "data" / "concept_layer.jsonl"
OUT = HERE / "wikidata_edges.jsonl"

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "WikiLean/0.1 (https://wikilean.jackmccarthy.org)"
BATCH = 100
PAUSE = 1.0
ENTITY_PREFIX = "http://www.wikidata.org/entity/"


def sparql_query(text: str) -> dict:
    data = urllib.parse.urlencode({"query": text}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as r:
        return json.loads(r.read(), strict=False)


def main() -> None:
    qids: list[str] = []
    seen: set[str] = set()
    with CONCEPT.open() as fh:
        for line in fh:
            q = json.loads(line).get("qid")
            if q and q not in seen:
                seen.add(q)
                qids.append(q)
    qid_set = set(qids)
    print(f"{len(qids)} unique QIDs")

    total = 0
    failed_batches: list[int] = []
    with OUT.open("w") as out:
        for i in range(0, len(qids), BATCH):
            batch = qids[i : i + BATCH]
            values = " ".join(f"wd:{q}" for q in batch)
            sparql = f"""
SELECT ?s ?p ?pLabel ?o WHERE {{
  VALUES ?s {{ {values} }}
  ?s ?pd ?o .
  ?p wikibase:directClaim ?pd .
  FILTER(isIRI(?o) && STRSTARTS(STR(?o), "{ENTITY_PREFIX}Q"))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""
            res = None
            for attempt in range(3):
                try:
                    res = sparql_query(sparql)
                    break
                except Exception as e:
                    wait = 5 * (attempt + 1)
                    print(f"  batch {i}: {e}; retry in {wait}s")
                    time.sleep(wait)
            if res is None:
                print(f"  batch {i}: FAILED after 3 attempts; skipping")
                failed_batches.append(i)
                continue

            kept = 0
            for b in res["results"]["bindings"]:
                o = b["o"]["value"].rsplit("/", 1)[-1]
                if o not in qid_set:
                    continue
                s = b["s"]["value"].rsplit("/", 1)[-1]
                p = b["p"]["value"].rsplit("/", 1)[-1]
                plab = b.get("pLabel", {}).get("value", "")
                out.write(json.dumps({"s": s, "p": p, "p_label": plab, "o": o}) + "\n")
                kept += 1
                total += 1
            print(f"  batch {i:>5}: {len(batch)} subj -> {kept} kept (total {total})")
            time.sleep(PAUSE)

    print(f"wrote {total} edges -> {OUT}")
    if failed_batches:
        print(f"WARN: {len(failed_batches)} batches skipped: {failed_batches}")


if __name__ == "__main__":
    main()
