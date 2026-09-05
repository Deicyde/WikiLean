#!/usr/bin/env python3
"""Pull a Wikidata-centric universe of math QIDs.

For each math class in CLASSES, queries WDQS for entities whose direct P31 is
that class. Collects qid + English label + enwiki article slug (if any).

Per-class queries are accumulated in memory and published only after every
request succeeds. Output:
  catalog/data/wikidata_universe.jsonl

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
OUT = HERE.parent / "data" / "wikidata_universe.jsonl"
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
PAUSE = 2.0
RETRIES = 3
RETRY_DELAY = 5.0
QID_RE = re.compile(r"Q[1-9][0-9]*\Z")
ENTITY_PREFIX = "http://www.wikidata.org/entity/"

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


def _bindings(response: dict) -> list[dict]:
    try:
        bindings = response["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("WDQS response lacks results.bindings") from exc
    if not isinstance(bindings, list):
        raise RuntimeError("WDQS results.bindings must be a list")
    return bindings


def _qid(uri: object, *, field: str) -> str:
    if not isinstance(uri, str):
        raise RuntimeError(f"WDQS {field} URI must be a string")
    if not uri.startswith(ENTITY_PREFIX):
        raise RuntimeError(f"WDQS {field} is not a Wikidata entity URI: {uri!r}")
    qid = uri.removeprefix(ENTITY_PREFIX)
    if not QID_RE.fullmatch(qid):
        raise RuntimeError(f"WDQS {field} is not a canonical QID: {uri!r}")
    return qid


def load_prior_counts() -> tuple[int, dict[str, int], int, int] | None:
    """Parse the prior generation only as volume-validation evidence."""
    if not OUT.exists():
        return None
    qids: set[str] = set()
    class_members: dict[str, set[str]] = {qid: set() for qid in CLASSES}
    labeled = 0
    with_slug = 0
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
            if set(row) != {"qid", "label", "classes", "enwiki_slug"}:
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid fields"
                )
            qid = row.get("qid")
            if not isinstance(qid, str) or not QID_RE.fullmatch(qid):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid qid"
                )
            if qid in qids:
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: duplicate {qid}"
                )
            classes = row.get("classes")
            if not isinstance(classes, list) or any(
                not isinstance(value, str) or not QID_RE.fullmatch(value)
                for value in classes
            ) or len(set(classes)) != len(classes):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid classes"
                )
            if not isinstance(row.get("label"), str) or not (
                row.get("enwiki_slug") is None
                or isinstance(row.get("enwiki_slug"), str)
            ):
                raise RuntimeError(
                    f"cannot validate prior {OUT.name}:{line_number}: invalid text fields"
                )
            if row["label"] and row["label"] != qid:
                labeled += 1
            if row["enwiki_slug"]:
                with_slug += 1
            qids.add(qid)
            for class_qid in set(classes):
                if class_qid in class_members:
                    class_members[class_qid].add(qid)
    if not qids:
        raise RuntimeError(f"cannot validate empty prior {OUT.name}")
    return (
        len(qids),
        {
            class_qid: len(members)
            for class_qid, members in class_members.items()
        },
        labeled,
        with_slug,
    )


def validate_volume(
    rows: list[dict],
    prior: tuple[int, dict[str, int], int, int] | None,
) -> None:
    """Reject total or per-class collapses without importing prior content."""
    class_counts = {class_qid: 0 for class_qid in CLASSES}
    for row in rows:
        for class_qid in row["classes"]:
            if class_qid in class_counts:
                class_counts[class_qid] += 1
    if prior is None:
        for class_qid, class_name in CLASSES.items():
            require_volume(
                artifact=f"{OUT.name} class {class_name} ({class_qid})",
                actual=class_counts[class_qid],
                floor=1,
            )
        if len(rows) >= 50:
            require_volume(
                artifact=f"{OUT.name} non-QID labels",
                actual=sum(
                    1 for row in rows
                    if row["label"] and row["label"] != row["qid"]
                ),
                floor=min(50, len(rows) // 2),
            )
            require_volume(
                artifact=f"{OUT.name} enwiki slugs",
                actual=sum(1 for row in rows if row["enwiki_slug"]),
                floor=min(50, len(rows) // 4),
            )
        return
    previous_total, previous_classes, previous_labels, previous_slugs = prior
    require_volume(
        artifact=OUT.name,
        actual=len(rows),
        floor=conservative_volume_floor(previous_total),
    )
    for class_qid, class_name in CLASSES.items():
        previous = previous_classes[class_qid]
        require_volume(
            artifact=f"{OUT.name} class {class_name} ({class_qid})",
            actual=class_counts[class_qid],
            floor=max(1, conservative_volume_floor(previous)),
        )
    require_volume(
        artifact=f"{OUT.name} non-QID labels",
        actual=sum(
            1 for row in rows if row["label"] and row["label"] != row["qid"]
        ),
        floor=conservative_volume_floor(previous_labels),
    )
    require_volume(
        artifact=f"{OUT.name} enwiki slugs",
        actual=sum(1 for row in rows if row["enwiki_slug"]),
        floor=conservative_volume_floor(previous_slugs),
    )


def fetch_universe() -> list[dict]:
    entities: dict[str, dict[str, set[str]]] = {}

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
        res = None
        for attempt in range(RETRIES):
            try:
                res = query(sparql)
                break
            except Exception as exc:  # noqa: BLE001 — retry, then fail closed
                if attempt == RETRIES - 1:
                    raise RuntimeError(
                        f"{cls_name} ({cls_qid}) failed after {RETRIES} attempts"
                    ) from exc
                wait = RETRY_DELAY * (attempt + 1)
                print(f"  retry {attempt + 1} after {type(exc).__name__}",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
        assert res is not None

        n = 0
        for binding in _bindings(res):
            if not isinstance(binding, dict):
                raise RuntimeError("WDQS binding must be an object")
            try:
                ent_qid = _qid(binding["x"]["value"], field="x")
            except (KeyError, TypeError) as exc:
                raise RuntimeError("WDQS binding lacks x.value") from exc
            label = binding.get("xLabel", {}).get("value", ent_qid)
            if not isinstance(label, str):
                raise RuntimeError(f"WDQS label for {ent_qid} must be a string")
            slug: str | None = None
            if "article" in binding:
                try:
                    url = binding["article"]["value"]
                except (KeyError, TypeError) as exc:
                    raise RuntimeError(
                        f"WDQS article for {ent_qid} lacks a value"
                    ) from exc
                if not isinstance(url, str):
                    raise RuntimeError(
                        f"WDQS article for {ent_qid} must be a string"
                    )
                prefix = "https://en.wikipedia.org/wiki/"
                if not url.startswith(prefix):
                    raise RuntimeError(
                        f"WDQS article for {ent_qid} is not an enwiki URI: {url!r}"
                    )
                slug = urllib.parse.unquote(url.removeprefix(prefix))
            ent = entities.setdefault(ent_qid, {
                "labels": set(),
                "classes": set(),
                "slugs": set(),
            })
            ent["labels"].add(label)
            ent["classes"].add(cls_qid)
            if slug:
                ent["slugs"].add(slug)
            n += 1
        print(f"  -> {n} bindings; {len(entities)} unique entities so far", flush=True)
        time.sleep(PAUSE)

    if not entities:
        raise RuntimeError("refusing to publish an empty Wikidata universe")
    rows = []
    for qid in sorted(entities, key=lambda value: (len(value), value)):
        entity = entities[qid]
        labels = entity["labels"] - {qid}
        rows.append({
            "qid": qid,
            "label": min(labels) if labels else qid,
            "classes": sorted(entity["classes"], key=lambda value: (len(value), value)),
            "enwiki_slug": min(entity["slugs"]) if entity["slugs"] else None,
        })
    return rows


def main() -> int:
    prior = None if force_publish_enabled() else load_prior_counts()
    rows = fetch_universe()
    validate_volume(rows, prior)
    atomic_write_bytes(OUT, canonical_jsonl_bytes(rows))

    print(f"\nwrote {len(rows)} entities -> {OUT}")
    with_enwiki = sum(1 for entity in rows if entity["enwiki_slug"])
    print(f"  with enwiki article: {with_enwiki}")
    print(f"  wikidata-only:        {len(rows) - with_enwiki}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
