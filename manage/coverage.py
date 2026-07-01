#!/usr/bin/env python3
"""Live coverage — the annotation half of WikiLean's control plane.

Reads site/annotations/*.json (the annotation layer, kept fresh from the
canonical D1 store by ``npm run pull``) and writes manage/data/coverage.json:
per-article statement counts and a coverage fraction, joined to a Wikidata QID.

Coverage is per-*statement* (how many of an article's annotations are
formalized), a finer signal than the concept graph's per-QID "is it mapped"
status. ``coverage = (formalized + 0.5*partial) / total`` over non-rejected
annotations; an article with no annotation file is reported as coverage 0 with
``annotated=False`` so the moderation worklist can surface never-touched pages.

Pure Python, deterministic, no network, no LLM.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
ANN_DIR = ROOT / "site" / "annotations"
ARTICLES = ROOT / "catalog" / "data" / "articles.jsonl"
GRAPH = ROOT / "catalog" / "data" / "concept_graph.json"
OUT = HERE / "data" / "coverage.json"

# Statuses that count toward totals; "rejected" (human tombstone) is excluded.
_COUNTED = ("formalized", "partial", "not_formalized")


def slug_to_qid() -> dict:
    """Authoritative slug->QID join: concept graph first, then articles.jsonl."""
    m: dict[str, str] = {}
    if ARTICLES.exists():
        for line in ARTICLES.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            title, qid = r.get("title"), r.get("wikidata_qid")
            if title and qid:
                m.setdefault(title.replace(" ", "_"), qid)
    if GRAPH.exists():
        g = json.loads(GRAPH.read_text())
        for n in g.get("nodes", []):
            if n.get("slug") and n.get("qid"):
                # setdefault, NOT overwrite: articles.jsonl (the real corpus
                # article->QID) wins on a collision. Two graph nodes can share a
                # slug (Q199 "1" and Q310395 "-1", whose "-" was stripped) — a
                # last-write-wins here would silently mis-assign slug "1" to -1.
                m.setdefault(n["slug"], n["qid"])
    return m


def _counts(annotations) -> dict:
    """Status-aware counts.

    Annotations with no ``status`` are Agent-1 extractions (provenance
    ``ai-agent1``) that never went through Agent-2 formalization. We must NOT
    conflate those with "unannotated": an article with extracted-but-unstatused
    statements is a distinct state ("awaiting formalization"), and a valuable
    one — the statements are already found. ``state``:
      empty      — no annotations at all
      extracted  — statements extracted, none formalized yet (awaiting Agent 2)
      moderated  — has status-bearing annotations
    """
    c = {"formalized": 0, "partial": 0, "not_formalized": 0, "rejected": 0}
    n_ann = len(annotations or [])
    n_nostatus = 0
    for a in annotations or []:
        s = a.get("status")
        if s in c:
            c[s] += 1
        elif s is None:
            n_nostatus += 1
    n_status = c["formalized"] + c["partial"] + c["not_formalized"]
    cov = (c["formalized"] + 0.5 * c["partial"]) / n_status if n_status else 0.0
    state = "empty" if n_ann == 0 else "extracted" if n_status == 0 else "moderated"
    return {
        "n_formalized": c["formalized"],
        "n_partial": c["partial"],
        "n_not_formalized": c["not_formalized"],
        "n_rejected": c["rejected"],
        "n_annotations": n_ann,
        "n_extracted_only": n_nostatus,
        "n_status": n_status,
        "coverage": round(cov, 4),
        "state": state,
    }


def compute() -> dict:
    s2q = slug_to_qid()
    per_slug: dict[str, dict] = {}
    newest_mtime = 0.0
    for path in sorted(ANN_DIR.glob("*.json")):
        name = path.name
        if name.endswith(".agent1.json") or name.startswith("."):
            continue
        slug = name[: -len(".json")]
        try:
            doc = json.loads(path.read_text())
        except Exception:
            continue
        rec = _counts(doc.get("annotations"))
        rec["slug"] = slug
        rec["qid"] = s2q.get(slug)
        rec["annotated"] = rec["n_status"] > 0
        per_slug[slug] = rec
        newest_mtime = max(newest_mtime, path.stat().st_mtime)

    tot = {k: sum(r[k] for r in per_slug.values())
           for k in ("n_formalized", "n_partial", "n_not_formalized")}
    grand = sum(tot.values())
    extracted = [r for r in per_slug.values() if r["state"] == "extracted"]
    return {
        "n_articles": len(per_slug),
        "totals": {
            **tot,
            "grand": grand,
            "pct_formalized": round(100 * tot["n_formalized"] / grand, 1) if grand else 0,
            "pct_partial": round(100 * tot["n_partial"] / grand, 1) if grand else 0,
            "pct_not_formalized": round(100 * tot["n_not_formalized"] / grand, 1) if grand else 0,
        },
        "backlog": {
            "extracted_articles": len(extracted),
            # the awaiting-Agent-2 statements are the status-less ones, NOT every
            # annotation (an extracted article could also carry human tombstones).
            "extracted_statements": sum(r["n_extracted_only"] for r in extracted),
            "empty_articles": sum(1 for r in per_slug.values() if r["state"] == "empty"),
        },
        "annotations_mtime": newest_mtime,
        "by_slug": per_slug,
    }


def main() -> None:
    result = compute()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    t, b = result["totals"], result["backlog"]
    print(f"coverage: {result['n_articles']} articles, {t['grand']} status-bearing annotations -> {OUT.relative_to(ROOT)}")
    print(f"  {t['pct_formalized']}% formalized / {t['pct_partial']}% partial / "
          f"{t['pct_not_formalized']}% not formalized")
    print(f"  backlog: {b['extracted_articles']} articles ({b['extracted_statements']} statements) "
          f"awaiting Agent-2 formalization; {b['empty_articles']} empty")


if __name__ == "__main__":
    main()
