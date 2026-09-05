#!/usr/bin/env python3
"""Pull the Wikidata induced subgraph over the concept-layer QIDs.

For each batch of subject QIDs, asks WDQS for all outgoing direct-claim
statements whose object is a Wikidata item, then keeps only the edges whose
object is also one of our QIDs. Filtering in Python instead of inside SPARQL
keeps the query small and predictable.

Output: wikidata_edges.jsonl, one edge per line:
  {"s": "Q...", "p": "P...", "p_label": "...", "o": "Q..."}

An intentional reviewed volume collapse requires BRAIN_INGEST_FORCE=1.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
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
# the brain's full concept pool (2.6k+ QIDs) — the v1 concept layer alone left
# the relates layer at ~1k edges over a 1,376-QID subset
BRAIN_NODES = HERE.parent.parent / "brain" / "data" / "nodes.jsonl"
OUT = HERE / "wikidata_edges.jsonl"
sys.path.insert(0, str(HERE.parent))
from wikidata_publish import (  # noqa: E402
    atomic_write_bytes,
    canonical_jsonl_bytes,
    conservative_volume_floor,
    force_publish_enabled,
    require_volume,
)

ENDPOINT = "https://query.wikidata.org/sparql"
UA = "WikiLean/0.1 (https://wikilean.jackmccarthy.org)"
BATCH = 100
PAUSE = 1.0
RETRIES = 3
RETRY_DELAY = 5.0
ENTITY_PREFIX = "http://www.wikidata.org/entity/"
QID_RE = re.compile(r"Q[1-9][0-9]*\Z")
PID_RE = re.compile(r"P[1-9][0-9]*\Z")


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


def _canonical_qid(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not QID_RE.fullmatch(value):
        raise RuntimeError(f"{location} is not a canonical QID: {value!r}")
    return value


def _entity_id(uri: object, *, location: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(uri, str) or not uri.startswith(ENTITY_PREFIX):
        raise RuntimeError(f"{location} is not a Wikidata entity URI: {uri!r}")
    value = uri.removeprefix(ENTITY_PREFIX)
    if not pattern.fullmatch(value):
        raise RuntimeError(f"{location} has a noncanonical identifier: {uri!r}")
    return value


def load_qids() -> list[str]:
    qids: set[str] = set()
    with CONCEPT.open() as fh:
        for line_number, line in enumerate(fh, 1):
            row = json.loads(line)
            qid = row.get("qid")
            if qid:
                qids.add(_canonical_qid(
                    qid,
                    location=f"{CONCEPT.name}:{line_number}.qid",
                ))
    if BRAIN_NODES.exists():
        with BRAIN_NODES.open() as fh:
            for line_number, line in enumerate(fh, 1):
                row = json.loads(line)
                qid = row.get("id")
                if row.get("type") == "concept" and qid:
                    qids.add(_canonical_qid(
                        qid,
                        location=f"{BRAIN_NODES.name}:{line_number}.id",
                    ))
    return sorted(qids, key=lambda value: (len(value), value))


def _bindings(response: dict) -> list[dict]:
    try:
        bindings = response["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("WDQS response lacks results.bindings") from exc
    if not isinstance(bindings, list):
        raise RuntimeError("WDQS results.bindings must be a list")
    return bindings


def load_prior_count() -> tuple[int, int] | None:
    """Parse the prior artifact only to establish a trustworthy row baseline."""
    if not OUT.exists():
        return None
    seen: set[tuple[str, str, str]] = set()
    labeled = 0
    with OUT.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"cannot validate malformed prior {OUT.name}:{line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: row is not an object"
                )
            if set(row) != {"s", "p", "p_label", "o"}:
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid fields"
                )
            subject = row.get("s")
            predicate = row.get("p")
            obj = row.get("o")
            if not isinstance(subject, str) or not QID_RE.fullmatch(subject):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid subject"
                )
            if not isinstance(predicate, str) or not PID_RE.fullmatch(predicate):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid predicate"
                )
            if not isinstance(obj, str) or not QID_RE.fullmatch(obj):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid object"
                )
            if not isinstance(row.get("p_label"), str):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid label"
                )
            if row["p_label"] and row["p_label"] != predicate:
                labeled += 1
            key = (subject, predicate, obj)
            if key in seen:
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: duplicate edge"
                )
            seen.add(key)
    return len(seen), labeled


def validate_volume(
    rows: list[dict],
    qid_count: int,
    prior_count: tuple[int, int] | None,
) -> None:
    """Reject source collapse while permitting genuinely small fresh fixtures."""
    if prior_count:
        previous_rows, previous_labels = prior_count
        floor = conservative_volume_floor(previous_rows)
    elif qid_count >= 50:
        floor = min(50, qid_count // 2)
    else:
        floor = 0
    require_volume(artifact=OUT.name, actual=len(rows), floor=floor)
    if prior_count:
        require_volume(
            artifact=f"{OUT.name} predicate labels",
            actual=sum(
                1 for row in rows
                if row["p_label"] and row["p_label"] != row["p"]
            ),
            floor=conservative_volume_floor(previous_labels),
        )
    elif len(rows) >= 50:
        require_volume(
            artifact=f"{OUT.name} predicate labels",
            actual=sum(
                1 for row in rows
                if row["p_label"] and row["p_label"] != row["p"]
            ),
            floor=min(50, len(rows) // 2),
        )


def fetch_edges(qids: list[str]) -> list[dict]:
    if not qids:
        raise RuntimeError("refusing to publish Wikidata edges for an empty QID set")
    qid_set = set(qids)
    print(f"{len(qids)} unique QIDs")

    edges: dict[tuple[str, str, str], set[str]] = {}
    for i in range(0, len(qids), BATCH):
        batch = qids[i : i + BATCH]
        batch_set = set(batch)
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
        response = None
        for attempt in range(RETRIES):
            try:
                response = sparql_query(sparql)
                break
            except Exception as exc:  # noqa: BLE001 — retry, then fail closed
                if attempt == RETRIES - 1:
                    raise RuntimeError(
                        f"batch {i} failed after {RETRIES} attempts"
                    ) from exc
                wait = RETRY_DELAY * (attempt + 1)
                print(f"  batch {i}: {exc}; retry in {wait}s")
                time.sleep(wait)
        assert response is not None

        kept = 0
        for binding in _bindings(response):
            if not isinstance(binding, dict):
                raise RuntimeError("WDQS binding must be an object")
            try:
                subject_uri = binding["s"]["value"]
                predicate_uri = binding["p"]["value"]
                object_uri = binding["o"]["value"]
            except (KeyError, TypeError) as exc:
                raise RuntimeError("WDQS edge binding is incomplete") from exc
            subject = _entity_id(
                subject_uri, location="WDQS subject", pattern=QID_RE
            )
            predicate = _entity_id(
                predicate_uri, location="WDQS predicate", pattern=PID_RE
            )
            obj = _entity_id(object_uri, location="WDQS object", pattern=QID_RE)
            if subject not in batch_set:
                raise RuntimeError(
                    f"WDQS returned subject {subject} outside the requested batch"
                )
            if obj not in qid_set:
                continue
            label = binding.get("pLabel", {}).get("value", "")
            if not isinstance(label, str):
                raise RuntimeError(
                    f"WDQS predicate label for {predicate} must be a string"
                )
            edges.setdefault((subject, predicate, obj), set()).add(label)
            kept += 1
        print(f"  batch {i:>5}: {len(batch)} subj -> {kept} kept "
              f"({len(edges)} unique total)")
        time.sleep(PAUSE)

    rows = []
    for (subject, predicate, obj), labels in sorted(edges.items()):
        nonempty_labels = labels - {"", predicate}
        rows.append({
            "s": subject,
            "p": predicate,
            "p_label": min(nonempty_labels) if nonempty_labels else "",
            "o": obj,
        })
    return rows


def main() -> int:
    prior_count = None if force_publish_enabled() else load_prior_count()
    qids = load_qids()
    rows = fetch_edges(qids)
    validate_volume(rows, len(qids), prior_count)
    atomic_write_bytes(OUT, canonical_jsonl_bytes(rows))
    print(f"wrote {len(rows)} edges -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
