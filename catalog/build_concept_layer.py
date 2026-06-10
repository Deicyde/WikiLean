#!/usr/bin/env python3
"""Build the WikiLean concept layer — the per-Wikidata-entity formalization map.

This is the second WikiLean data layer, complementary to the article-level W3C
annotation layer:

  - annotation layer : per ARTICLE, span-level (site/annotations/<slug>.json),
                       keyed by slug + text anchor. Needs a Wikipedia article.
  - concept layer    : per WIKIDATA ENTITY (this file), one record per QID
                       mapping the concept to its primary Mathlib declaration.
                       Keyed by QID; can later cover concepts with no article.

The two link via QID ↔ article_slug. The concept layer is the single source of
truth for the QID→Mathlib mapping (export_wikidata_rdf.py reads it).

Seed source: the AI-tagged high-value subset (pilot_tagged + tier2_tagged),
deduped by QID (924 QIDs are shared across multiple article titles — those
collapse to one concept here). Records are provenance-tagged "ai"; human review
can upgrade them in place later.

    python build_concept_layer.py            # → data/concept_layer.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = DATA / "concept_layer.jsonl"
TAGGED = ["pilot_tagged.jsonl", "tier2_tagged.jsonl"]


def make_slug(title: str) -> str:
    """Match the annotation layer's slugging so article_slug lines up with
    site/annotations/<slug>.json. 'Picard–Lindelöf theorem' → 'Picard-Lindelof_theorem'."""
    s = title.replace("–", "-").replace("—", "-")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_.\-]", "", s)


def primary_module(rec: dict) -> tuple[str | None, str | None]:
    """(module, confidence) of the record's primary decl, from its matched decls."""
    primary = rec.get("primary_decl")
    for d in rec.get("mathlib_decls") or []:
        if d.get("decl") == primary:
            return d.get("module"), d.get("confidence")
    decls = rec.get("mathlib_decls") or []
    if decls:
        return decls[0].get("module"), decls[0].get("confidence")
    return None, None


def merge(existing: dict, rec: dict) -> dict:
    """Merge a second tagged row sharing the same QID. Prefer the formalized
    one as the canonical record; always accumulate the title."""
    cand = build_record(rec)
    if existing is None:
        return cand
    # Accumulate titles.
    titles = list(dict.fromkeys(existing["titles"] + cand["titles"]))
    winner = existing
    # A formalized record beats a not-formalized one.
    if cand["status"] == "formalized" and existing["status"] != "formalized":
        winner = cand
    winner = {**winner, "titles": titles}
    return winner


def build_record(rec: dict) -> dict:
    title = rec["title"]
    module, confidence = primary_module(rec)
    primary = rec.get("primary_decl")
    status = "formalized" if primary else "not_formalized"
    secondary = [
        {"decl": d.get("decl"), "module": d.get("module")}
        for d in (rec.get("mathlib_decls") or [])
        if d.get("decl") and d.get("decl") != primary
    ]
    return {
        "qid": rec.get("wikidata_qid"),
        "titles": [title],
        "primary_title": title,
        "article_slug": make_slug(title),
        "class": rec.get("class"),
        "importance": rec.get("importance"),
        "status": status,
        "primary_decl": primary,
        "module": module,
        "confidence": confidence,
        "secondary_decls": secondary,
        "no_match_reason": rec.get("no_match_reason") if not primary else None,
        "provenance": "ai",
        "source": "wikilean-tagging",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    # Load tagged rows by title (last-wins per title).
    by_title: dict[str, dict] = {}
    for f in TAGGED:
        p = DATA / f
        if not p.exists():
            continue
        for line in p.open():
            r = json.loads(line)
            by_title[r["title"]] = r

    # Collapse to one concept record per QID.
    by_qid: dict[str, dict] = {}
    no_qid = 0
    for rec in by_title.values():
        qid = rec.get("wikidata_qid")
        if not qid:
            no_qid += 1
            continue
        by_qid[qid] = merge(by_qid.get(qid), rec)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_formalized = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for qid, rec in sorted(by_qid.items()):
            rec["built_at"] = stamp
            if rec["status"] == "formalized":
                n_formalized += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"wrote {args.out}")
    print(f"  concepts (by QID): {len(by_qid)}")
    print(f"    formalized:      {n_formalized}")
    print(f"    not_formalized:  {len(by_qid) - n_formalized}")
    print(f"  tagged rows dropped (no QID): {no_qid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
