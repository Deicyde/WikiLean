#!/usr/bin/env python3
"""One-SPARQL crossref backfill: for every QID in the concept layer (v1 graph
∪ v2 graph), fetch its external-database identifiers from Wikidata (the math
crossref property family) and write catalog/data/wikidata_crossrefs.json.

Zero new join infrastructure — Wikidata is the hub WikiLean already keys on.
The graph builder stamps these as per-node chips (MathWorld, nLab, ProofWiki,
Metamath, LMFDB knowl, OEIS, …), making /graph a multi-database join surface.

Deterministic (no LLM). Atomic write: on any fetch failure the previous file
survives (the nightly is fail-soft by design). Chunked VALUES queries stay well
under WDQS limits. Run: python3 catalog/mathlib_deps/fetch_crossrefs.py
"""
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
CONCEPT_GRAPH = DATA / "concept_graph.json"
CONCEPT_GRAPH_V2 = DATA / "concept_graph_v2.json"
SOURCE_REGISTRY = DATA / "source_registry.json"
OUT = DATA / "wikidata_crossrefs.json"
WDQS = "https://query.wikidata.org/sparql"
UA = "WikiLean/1.0 (https://wikilean.jackmccarthy.org)"

CHUNK = 250
RETRIES = 3


def load_props() -> dict[str, str]:
    """PID → lowercase-canonical key, derived from source_registry.json
    crossref_sources (the provenance single-source-of-truth). kgmid carries two
    PIDs slash-joined ("P2671/P646") — kgmids come from Wikidata (CC0), never
    Google's API (ToS §5.e). NB literature props (P818 arXiv) deliberately
    absent from the registry: they live on the scholarly WDQS split."""
    sources = json.loads(SOURCE_REGISTRY.read_text())["crossref_sources"]
    return {pid: key for key, entry in sources.items()
            for pid in entry["wikidata_property"].split("/")}


def load_qids() -> list[str]:
    qids: set[str] = set()
    for graph in (CONCEPT_GRAPH, CONCEPT_GRAPH_V2):
        qids |= {n["qid"] for n in json.loads(graph.read_text())["nodes"]
                 if isinstance(n.get("qid"), str) and n["qid"].startswith("Q")}
    return sorted(qids)


def sparql(query: str) -> list[dict]:
    # curl, not urllib: the system Python's SSL trust store is broken on this
    # machine (same reason bot/pool.py shells out to curl for the Wikidata API).
    url = WDQS + "?" + urllib.parse.urlencode({"query": query})
    for attempt in range(RETRIES):
        try:
            out = subprocess.run(
                ["curl", "-sS", "-m", "90", "--retry", "2",
                 "-H", "Accept: application/sparql-results+json",
                 "-H", f"User-Agent: {UA}", url],
                capture_output=True, text=True, timeout=120, check=True).stdout
            return json.loads(out)["results"]["bindings"]
        except Exception as e:  # noqa: BLE001 — retry then re-raise
            if attempt == RETRIES - 1:
                raise
            print(f"  retry {attempt + 1} after {type(e).__name__}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return []


def main() -> int:
    props = load_props()
    qids = load_qids()
    print(f"{len(qids)} concept QIDs → {len(props)} crossref properties")
    props_clause = " ".join(f"wdt:{p}" for p in props)
    xrefs: dict[str, dict[str, list[str]]] = {}
    n_pairs = 0
    for i in range(0, len(qids), CHUNK):
        chunk = qids[i:i + CHUNK]
        values = " ".join(f"wd:{q}" for q in chunk)
        rows = sparql(
            f"SELECT ?item ?prop ?v WHERE {{ VALUES ?item {{ {values} }} "
            f"VALUES ?prop {{ {props_clause} }} ?item ?prop ?v }}")
        for r in rows:
            q = r["item"]["value"].rsplit("/", 1)[1]
            pid = "P" + r["prop"]["value"].rsplit("/P", 1)[1]
            key = props.get(pid)
            if not key:
                continue
            xrefs.setdefault(q, {}).setdefault(key, []).append(str(r["v"]["value"]))
            n_pairs += 1
        print(f"  {min(i + CHUNK, len(qids))}/{len(qids)} … {len(xrefs)} QIDs matched")
        time.sleep(1)  # polite to WDQS
    for per_q in xrefs.values():  # deterministic output
        for k in per_q:
            per_q[k] = sorted(set(per_q[k]))
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"fetched_from": "query.wikidata.org", "properties": props, "xrefs": xrefs},
        ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(OUT)  # atomic — a failed run never truncates the live file
    multi = sum(1 for v in xrefs.values() if len(v) >= 2)
    print(f"wrote {OUT.name}: {len(xrefs)}/{len(qids)} QIDs have ≥1 crossref "
          f"({multi} multi-homed, {n_pairs} id pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
