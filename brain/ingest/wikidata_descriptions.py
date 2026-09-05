#!/usr/bin/env python3
"""Wikidata descriptions fetcher — catalog/data/wikidata_descriptions.json.

Collects every concept QID the Brain builds over (rebuild_grounding.json +
universe_extension.jsonl + wikidata_crossrefs.json), then fetches one coherent
snapshot via wbgetentities in batches of 50 (props=descriptions, languages=en,
CC0). Output shape (SCHEMA.md v2): {"_meta": {...}, "descriptions": {qid: text}}.
The previous output is never used as an input, and publication is atomic only
after every requested batch has succeeded.

An intentional reviewed volume collapse requires BRAIN_INGEST_FORCE=1.

Run: python3 brain/ingest/wikidata_descriptions.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
sys.path.insert(0, str(common.REPO / "catalog"))
from wikidata_publish import (  # noqa: E402
    atomic_write_bytes,
    canonical_json_bytes,
    conservative_volume_floor,
    force_publish_enabled,
    require_volume,
)

API = "https://www.wikidata.org/w/api.php"
OUT = common.REPO / "catalog" / "data" / "wikidata_descriptions.json"
GROUNDING = common.REPO / "catalog" / "data" / "rebuild_grounding.json"
UNIVERSE_EXT = common.REPO / "catalog" / "data" / "universe_extension.jsonl"
CROSSREFS = common.CROSSREFS
BATCH = 50
DELAY = 0.3
RETRIES = 3
RETRY_DELAY = 2.0
QID_RE = re.compile(r"Q[1-9][0-9]*\Z")


def _add_qid(qids: set[str], value: object, *, location: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not QID_RE.fullmatch(value):
        raise RuntimeError(f"{location} is not a canonical QID: {value!r}")
    qids.add(value)


def collect_qids() -> set[str]:
    """Return every concept QID in the three declared inputs."""
    qids: set[str] = set()
    for index, row in enumerate(json.loads(GROUNDING.read_text())):
        _add_qid(qids, row.get("qid"), location=f"{GROUNDING.name}[{index}].qid")
    with UNIVERSE_EXT.open() as f:
        for line_number, line in enumerate(f, 1):
            row = json.loads(line)
            _add_qid(
                qids,
                row.get("qid"),
                location=f"{UNIVERSE_EXT.name}:{line_number}.qid",
            )
    xrefs = json.loads(CROSSREFS.read_text()).get("xrefs", {})
    if not isinstance(xrefs, dict):
        raise RuntimeError(f"{CROSSREFS.name}.xrefs must be an object")
    for qid in xrefs:
        _add_qid(qids, qid, location=f"{CROSSREFS.name}.xrefs key")
    return qids


def fetch_batch(batch: list[str]) -> dict[str, str]:
    time.sleep(DELAY)
    url = API + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(batch),
        "props": "descriptions", "languages": "en", "format": "json",
    })
    data = json.loads(common.curl_fetch(url))
    if not isinstance(data, dict):
        raise RuntimeError("wbgetentities response must be an object")
    if "error" in data:
        raise RuntimeError("wbgetentities returned an error envelope")
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise RuntimeError("wbgetentities response lacks an entities object")
    if set(entities) != set(batch):
        missing = sorted(set(batch) - set(entities))
        extra = sorted(set(entities) - set(batch))
        raise RuntimeError(
            "wbgetentities returned an incomplete batch "
            f"(missing={missing}, extra={extra})"
        )
    out: dict[str, str] = {}
    for qid in batch:
        ent = entities[qid]
        if not isinstance(ent, dict):
            raise RuntimeError(f"wbgetentities entity {qid} must be an object")
        if "missing" in ent:
            if ent["missing"] != "":
                raise RuntimeError(
                    f"wbgetentities entity {qid} has an invalid missing marker"
                )
            continue
        entity_id = ent.get("id")
        if not isinstance(entity_id, str) or not QID_RE.fullmatch(entity_id):
            raise RuntimeError(
                f"wbgetentities entity {qid} lacks canonical item identity"
            )
        redirects = ent.get("redirects")
        if entity_id == qid:
            if redirects is not None:
                raise RuntimeError(
                    f"wbgetentities entity {qid} has an unexpected redirect object"
                )
        elif (
            not isinstance(redirects, dict)
            or redirects.get("from") != qid
            or redirects.get("to") != entity_id
        ):
            raise RuntimeError(
                f"wbgetentities entity {qid} has an invalid redirect identity"
            )
        if ent.get("type") != "item":
            raise RuntimeError(f"wbgetentities entity {qid} is not an item")
        entity_descriptions = ent.get("descriptions")
        if not isinstance(entity_descriptions, dict):
            raise RuntimeError(
                f"wbgetentities descriptions for {qid} must be an object"
            )
        english = entity_descriptions.get("en")
        if english is None:
            continue
        if not isinstance(english, dict):
            raise RuntimeError(
                f"wbgetentities English description for {qid} must be an object"
            )
        desc = english.get("value")
        if not isinstance(desc, str):
            raise RuntimeError(f"wbgetentities description for {qid} must be a string")
        if desc:
            out[qid] = desc
    return out


def load_prior_counts() -> tuple[int, int] | None:
    """Parse prior metadata and descriptions solely as a volume baseline."""
    if not OUT.exists():
        return None
    try:
        payload = json.loads(OUT.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cannot validate malformed prior {OUT.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"cannot validate prior {OUT.name}: root is not an object")
    if "descriptions" in payload:
        values = payload.get("descriptions")
        meta = payload.get("_meta")
        if not isinstance(values, dict) or not isinstance(meta, dict):
            raise RuntimeError(
                f"cannot validate prior {OUT.name}: invalid v2 envelope"
            )
        previous_qids = meta.get("n_qids")
        if (
            isinstance(previous_qids, bool)
            or not isinstance(previous_qids, int)
            or previous_qids < 0
        ):
            raise RuntimeError(
                f"cannot validate prior {OUT.name}: invalid _meta.n_qids"
            )
        declared_descriptions = meta.get("n_descriptions")
        if (
            isinstance(declared_descriptions, bool)
            or not isinstance(declared_descriptions, int)
            or declared_descriptions != len(values)
        ):
            raise RuntimeError(
                f"cannot validate prior {OUT.name}: invalid _meta.n_descriptions"
            )
    else:
        values = payload
        previous_qids = len(values)
    for qid, value in values.items():
        if not QID_RE.fullmatch(qid) or not isinstance(value, str):
            raise RuntimeError(
                f"cannot validate prior {OUT.name}: invalid description entry"
            )
    if len(values) > previous_qids:
        raise RuntimeError(
            f"cannot validate prior {OUT.name}: descriptions exceed n_qids"
        )
    return previous_qids, len(values)


def validate_volume(
    *,
    qid_count: int,
    description_count: int,
    prior: tuple[int, int] | None,
) -> None:
    """Scale the prior coverage to the current input population, then floor it."""
    if prior is None or prior[0] == 0 or prior[1] == 0:
        floor = min(50, qid_count) if qid_count >= 50 else 0
    else:
        previous_qids, previous_descriptions = prior
        expected = (
            previous_descriptions * qid_count + previous_qids - 1
        ) // previous_qids
        floor = min(qid_count, conservative_volume_floor(expected))
    require_volume(
        artifact=OUT.name,
        actual=description_count,
        floor=floor,
    )


def validate_qid_volume(
    qid_count: int,
    prior: tuple[int, int] | None,
) -> None:
    """Reject collapse of the input QID population before making requests."""
    if prior is None:
        return
    previous_qids, _previous_descriptions = prior
    require_volume(
        artifact=f"{OUT.name} input QID population",
        actual=qid_count,
        floor=conservative_volume_floor(previous_qids),
    )


def main() -> int:
    prior = None if force_publish_enabled() else load_prior_counts()
    qids = sorted(collect_qids(), key=lambda value: (len(value), value))
    if not qids:
        raise RuntimeError(
            "refusing to publish Wikidata descriptions for an empty QID set"
        )
    validate_qid_volume(len(qids), prior)
    descriptions: dict[str, str] = {}
    print(f"[wikidata_descriptions] fetching {len(qids)} QIDs", file=sys.stderr)
    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        for attempt in range(RETRIES):
            try:
                got = fetch_batch(batch)
                break
            except Exception as exc:  # noqa: BLE001 — retry, then fail closed
                if attempt == RETRIES - 1:
                    raise RuntimeError(
                        f"batch {i // BATCH} failed after {RETRIES} attempts"
                    ) from exc
                wait = RETRY_DELAY * (attempt + 1)
                print(f"[wikidata_descriptions] batch {i // BATCH} retry "
                      f"{attempt + 1} after {type(exc).__name__}", file=sys.stderr)
                time.sleep(wait)
        descriptions.update(got)

    validate_volume(
        qid_count=len(qids),
        description_count=len(descriptions),
        prior=prior,
    )
    payload = {
        "_meta": {
            "source": "wikidata wbgetentities (props=descriptions, languages=en)",
            "n_qids": len(qids),
            "n_descriptions": len(descriptions),
        },
        "descriptions": dict(sorted(descriptions.items())),
    }
    atomic_write_bytes(OUT, canonical_json_bytes(payload))
    print(f"[wikidata_descriptions] wrote {len(descriptions)} descriptions -> {OUT}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
