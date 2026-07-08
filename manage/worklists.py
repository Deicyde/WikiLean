#!/usr/bin/env python3
"""Worklists — join centrality × live coverage into actionable queues.

Consumes manage/data/{centrality,coverage}.json + bot/pool.py's eligibility
filter + catalog/data/articles.jsonl's cached P31 claims, and writes:

  manage/data/moderation_worklist.json
    formalize      — [extracted] articles: Agent-1 statements awaiting Agent-2.
                     Ranked by centrality. A real work queue — DRAINS once
                     formalized. Mission op (2).
    annotate       — [empty] articles + central concepts not mirrored in the
                     corpus, needing a first annotation pass. Ranked by
                     centrality. DRAINS once annotated. Mission op (1).
    coverage_gaps  — INFORMATIONAL map, not a queue: moderated central articles
                     with low coverage. Low coverage is often correct (the
                     concept simply isn't in Mathlib), and re-review priority for
                     *moderated* articles is a D1 signal (drift / flags /
                     staleness via /api/work), not a disk one — so this is a
                     "where is Mathlib thin?" map, not a to-do list. Biographies
                     (P31 = human) are excluded as noise.

  manage/data/pipeline_worklist.json
    pool candidates RE-RANKED by concept-graph centrality, with field/discipline
    QIDs excluded offline via cached articles.jsonl P31 claims — the same intent
    as pool's live P31 filter (which we can't call offline). Reports the
    wikilink-rank delta and how many field QIDs were excluded.

Pure Python, deterministic, offline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ARTICLES = ROOT / "catalog" / "data" / "articles.jsonl"
sys.path.insert(0, str(ROOT / "bot"))

# P31 values that mark a *field / area / theory* — not a taggable/annotatable
# math object. Superset of pool.FIELD_TYPES: adds Q20026918 "mathematical theory"
# (e.g. real analysis) which pool's live filter currently misses.
FIELD_P31 = {"Q1936384", "Q11862829", "Q2267705", "Q4671286", "Q1047113", "Q20026918"}
PERSON_P31 = {"Q5"}  # biographies — nothing to formalize

# How many items to keep per queue in the written artifact. Totals are always
# reported separately so a cap is never mistaken for the true count.
CAP = 200


def _load(name: str) -> dict:
    p = DATA / name
    if not p.exists():
        raise SystemExit(f"missing {p.relative_to(ROOT)} — run centrality.py and coverage.py first")
    return json.loads(p.read_text())


def load_p31() -> dict:
    """qid -> set(P31 values), from the cached corpus (offline)."""
    m: dict[str, set] = {}
    if ARTICLES.exists():
        for line in ARTICLES.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            q = r.get("wikidata_qid")
            if q:
                m[q] = set(r.get("p31") or [])
    return m


def moderation_worklist(centrality: dict, coverage: dict, p31: dict) -> dict:
    """Two drainable work queues + one informational coverage map."""
    scores = centrality["scores"]
    by_slug = coverage["by_slug"]
    formalize: dict[str, dict] = {}   # keyed by slug
    annotate: dict[str, dict] = {}    # keyed by slug (or qid if unmirrored)
    gaps: dict[str, dict] = {}        # keyed by slug

    def better(store, key, row):
        prev = store.get(key)
        if not prev or row["centrality_pct"] > prev["centrality_pct"]:
            store[key] = row

    for qid, c in scores.items():
        slug = c.get("slug")
        cen = c["centrality_pct"] / 100.0
        node_p31 = p31.get(qid, set())
        cov = by_slug.get(slug) if slug else None
        base = {"qid": qid, "label": c["label"], "slug": slug,
                "centrality_pct": c["centrality_pct"], "in_degree": c["in_degree"]}
        if cov:
            state = cov["state"]
            if state == "extracted":
                better(formalize, slug, {**base, "n_extracted": cov["n_extracted_only"],
                                         "score": round(cen, 4)})
            elif state == "empty":
                better(annotate, slug, {**base, "in_corpus": True, "score": round(cen, 4)})
            elif state == "moderated":
                if node_p31 & PERSON_P31:
                    continue  # a mathematician bio at 0% coverage is noise, not a gap
                gap = 1.0 - cov["coverage"]
                if gap <= 0:
                    continue
                better(gaps, slug, {**base, "coverage": cov["coverage"],
                                    "n_status": cov["n_status"], "score": round(cen * gap, 4)})
        else:  # central concept with no article on disk -> first-pass annotation (op 1)
            if node_p31 & PERSON_P31:
                continue
            better(annotate, slug or qid, {**base, "in_corpus": False, "score": round(cen, 4)})

    def pack(store):
        items = sorted(store.values(), key=lambda r: (-r["score"], -r["centrality_pct"]))
        return {"total": len(items), "shown": min(len(items), CAP), "items": items[:CAP]}

    return {"formalize": pack(formalize), "annotate": pack(annotate), "coverage_gaps": pack(gaps)}


def pipeline_worklist(centrality: dict, p31: dict) -> dict:
    import pool  # bot/pool.py

    scores = centrality["scores"]
    wikilink_order = list(json.loads(pool.MOST_USED.read_text()).keys())
    wikilink_rank = {q: i for i, q in enumerate(wikilink_order)}

    # All eligible candidates in the historical wikilink order; we apply the
    # field filter locally (offline) rather than pool's network P31 call. Keep
    # this off the Brain-ranked order so this artifact does not rank itself.
    cands = pool.candidates(n=10_000, require_high=True, p31_filter=False,
                            order_source="wikilink")

    kept, excluded_fields = [], 0
    for c in cands:
        aq, tq = c.get("article_qid"), c["qid"]
        if (p31.get(aq, set()) | p31.get(tq, set())) & FIELD_P31:
            excluded_fields += 1
            continue
        c["centrality_pct"] = scores.get(tq, {}).get("centrality_pct", 0.0)
        c["wikilink_rank"] = wikilink_rank.get(aq)
        kept.append(c)

    ranked = sorted(kept, key=lambda c: (-c["centrality_pct"], c.get("wikilink_rank") or 1e9))
    items = []
    for new_rank, c in enumerate(ranked[:CAP]):
        old = c["wikilink_rank"]
        items.append({
            "qid": c["qid"], "article_qid": c.get("article_qid"), "label": c["label"],
            "decl": c["decl"], "file": c["file"], "centrality_pct": c["centrality_pct"],
            "new_rank": new_rank, "wikilink_rank": old,
            "rank_delta": (old - new_rank) if old is not None else None,
        })
    return {"total": len(kept), "shown": len(items), "excluded_fields": excluded_fields, "items": items}


def main() -> None:
    centrality = _load("centrality.json")
    coverage = _load("coverage.json")
    p31 = load_p31()

    mod = moderation_worklist(centrality, coverage, p31)
    pipe = pipeline_worklist(centrality, p31)

    (DATA / "moderation_worklist.json").write_text(json.dumps(mod, ensure_ascii=False, indent=2))
    (DATA / "pipeline_worklist.json").write_text(json.dumps(pipe, ensure_ascii=False, indent=2))

    def head(q, n=6):
        return q["items"][:n]

    print("moderation worklist -> manage/data/moderation_worklist.json")
    f = mod["formalize"]
    print(f"  formalize (op 2, Agent-2 backlog): {f['total']} articles, showing {min(6, f['shown'])}")
    for r in head(f):
        print(f"    score={r['score']:.3f}  {r['qid']:<11} {r['label']:<28} {r['n_extracted']} stmts")
    a = mod["annotate"]
    print(f"  annotate (op 1, needs first pass): {a['total']} concepts, showing {min(6, a['shown'])}")
    for r in head(a):
        tag = "in-corpus" if r["in_corpus"] else "not mirrored"
        print(f"    cen={r['centrality_pct']:6.2f}  {r['qid']:<11} {r['label']:<28} [{tag}]")
    g = mod["coverage_gaps"]
    print(f"  coverage_gaps (informational map, not a queue): {g['total']} moderated articles")
    for r in head(g, 4):
        print(f"    score={r['score']:.3f}  cov={r['coverage']:.2f}  {r['qid']:<11} {r['label']}")

    print(f"\npipeline worklist -> manage/data/pipeline_worklist.json")
    print(f"  {pipe['total']} candidates ({pipe['excluded_fields']} field/theory QIDs excluded), showing 8:")
    for r in pipe["items"][:8]:
        d = r["rank_delta"]
        arrow = (f"(wikilink #{r['wikilink_rank']} {'↑' if d and d > 0 else '↓' if d and d < 0 else '='}{abs(d) if d else 0})"
                 if r["wikilink_rank"] is not None else "(new)")
        print(f"    cen={r['centrality_pct']:6.2f}  {r['qid']:<11} {r['label']:<30} {r['decl']:<22} {arrow}")


if __name__ == "__main__":
    main()
