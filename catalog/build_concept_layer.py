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

Output is clock/path independent and atomically replaced. Observation times
belong in acquisition evidence, not these normalized concept rows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import unicodedata
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


def build_rows(data_dir: Path = DATA) -> tuple[list[dict], int]:
    """Return deterministic concept rows and the number of unkeyed inputs."""
    data_dir = Path(data_dir)

    # Load tagged rows by title (last-wins per title).
    by_title: dict[str, dict] = {}
    for filename in TAGGED:
        path = data_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                by_title[record["title"]] = record

    # Collapse to one concept record per QID.
    by_qid: dict[str, dict] = {}
    no_qid = 0
    for record in by_title.values():
        qid = record.get("wikidata_qid")
        if not qid:
            no_qid += 1
            continue
        by_qid[qid] = merge(by_qid.get(qid), record)
    return [record for _qid, record in sorted(by_qid.items())], no_qid


def write_rows(path: Path, rows: list[dict]) -> None:
    """Atomically publish the deterministic JSONL bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            for record in rows:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    rows, no_qid = build_rows(args.data_dir)
    write_rows(args.out, rows)
    n_formalized = sum(record["status"] == "formalized" for record in rows)

    print(f"wrote {args.out}")
    print(f"  concepts (by QID): {len(rows)}")
    print(f"    formalized:      {n_formalized}")
    print(f"    not_formalized:  {len(rows) - n_formalized}")
    print(f"  tagged rows dropped (no QID): {no_qid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
