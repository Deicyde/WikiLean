#!/usr/bin/env python3
"""Deterministic candidate-tag pool selector (no LLM).

Walks a deterministic priority order and keeps the ones that have an
unambiguous, high-confidence Mathlib counterpart in the WikiLean catalog and are
NOT already tagged or in flight. By default that order is the Brain/control-plane
pipeline worklist (centrality-ranked), falling back to the old wikilink ranking.
These are the "unreviewed" tags for the queue / next batch.

Sources (all in-repo): catalog/data/{pilot,tier2}_tagged.jsonl,
bot/data/most_used_qids.json, bot/data/tagged_in_master.txt.
"""
import json, sys, argparse, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Later files win on a QID collision — refresh_tagged.jsonl holds re-tags from the
# improved agent (verified decls + tightest primary_qid) and overrides the originals.
CATALOG = [ROOT / "catalog/data/pilot_tagged.jsonl", ROOT / "catalog/data/tier2_tagged.jsonl",
           ROOT / "catalog/data/generated_candidates.jsonl",   # agent-generated, human-verified
           ROOT / "catalog/data/mathlib_yaml_tagged.jsonl",    # mathlib docs/1000.yaml, maintainer-reviewed (ingest_mathlib_yaml.py; new QIDs only, conflicts go to review)
           ROOT / "catalog/data/refresh_tagged.jsonl"]
MOST_USED = ROOT / "bot/data/most_used_qids.json"
TAGGED = ROOT / "bot/data/tagged_in_master.txt"
PIPELINE_WORKLIST = ROOT / "manage/data/pipeline_worklist.json"
WD_API = "https://www.wikidata.org/w/api.php"
# Tags per batch PR — the single knob for batch size. Mathlib maintainers asked
# for smaller batches (~10) over the original 25 (easier to review in one pass).
# open_batch.py / daily_bot.py both import this so there's one place to change it.
BATCH_SIZE = 10
# P31 ("instance of") values that mark a *field/discipline*, not a math OBJECT —
# we don't tag these (e.g. "linear algebra" Q82571, not the object "vector space").
FIELD_TYPES = {"Q1936384", "Q11862829", "Q2267705", "Q4671286", "Q1047113"}
SOURCE_TIER = {
    "mathlib_yaml_tagged.jsonl": "mathlib-maintainer",
    "refresh_tagged.jsonl": "ai-verified",
    "generated_candidates.jsonl": "ai-verified",
    "tier2_tagged.jsonl": "ai",
    "pilot_tagged.jsonl": "ai",
}


def field_of_math(qids):
    """Subset of qids whose Wikidata P31 marks them a field/discipline."""
    fields, qids = set(), list(qids)
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        url = (f"{WD_API}?action=wbgetentities&ids={'|'.join(chunk)}"
               f"&props=claims&format=json&origin=*")
        try:
            out = subprocess.run(["curl", "-s", "--retry", "3", "--retry-delay", "2", "-H", "User-Agent: WikiLean-bot/1.0", url],
                                 capture_output=True, text=True, timeout=40).stdout
            ents = json.loads(out).get("entities", {})
        except Exception:
            continue
        for q, e in ents.items():
            p31 = {c["mainsnak"]["datavalue"]["value"]["id"]
                   for c in e.get("claims", {}).get("P31", [])
                   if c["mainsnak"].get("datavalue")}
            if p31 & FIELD_TYPES:
                fields.add(q)
    return fields


def module_to_file(module):
    return module.replace(".", "/") + ".lean" if module else None


STATE = ROOT / "bot" / "state"


def seen_qids():
    """Every QID already put in front of reviewers, so it is NOT 'unreviewed':
    proposed in any past batch (batch*_approved.json), carried in the correction
    queue (recycle_queue.json — both the original and the corrected QID), or cut
    (cut_log.json). Merged tags are handled separately via tagged_in_master.txt.
    Without this the pool re-surfaces concepts reviewers already judged."""
    s = set()
    for f in STATE.glob("batch*_approved.json"):
        try:
            s |= {t["qid"] for t in json.loads(f.read_text()).get("tags", [])}
        except Exception:
            pass
    rq = STATE / "recycle_queue.json"
    if rq.exists():
        try:
            for e in json.loads(rq.read_text()):
                s.add(e["qid"])
                sq = e.get("triage", {}).get("suggested_qid")
                if sq:
                    s.add(sq)
        except Exception:
            pass
    cl = STATE / "cut_log.json"
    if cl.exists():
        try:
            s |= {e["qid"] for e in json.loads(cl.read_text())}
        except Exception:
            pass
    return s


def load_catalog():
    cat = {}
    for f in CATALOG:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            q, pd = r.get("wikidata_qid"), r.get("primary_decl")
            if not q or not pd:
                continue
            # tag_qid is the most-specific QID for the decl (the improved agent's
            # primary_qid); fall back to the article QID for legacy entries.
            tag_qid = r.get("primary_qid") or q
            file, conf = None, None
            for d in r.get("mathlib_decls", []):
                if d.get("decl") == pd:
                    file, conf = module_to_file(d.get("module")), d.get("confidence")
                    break
            # Label the TAG's QID, not the source article: when the agent narrowed
            # to a more specific QID (e.g. "Three-dimensional space" article ->
            # EuclideanSpace -> Q17295 "Euclidean space"), show that QID's label.
            label = r.get("primary_qid_label") or r.get("title", "")
            cat[q] = {"tag_qid": tag_qid, "decl": pd, "file": file,
                      "label": label, "confidence": conf,
                      "provenance_tier": SOURCE_TIER.get(f.name, "ai"),
                      "source_file": f.name}
    return cat


def load_pipeline_worklist():
    """Return (article-order, metadata-by-tag-qid) from the Brain worklist.

    The worklist is itself built from pool.candidates(..., order_source='wikilink')
    in manage/worklists.py, so this reader is intentionally fail-soft: a missing
    or stale worklist simply restores the historical wikilink ordering.
    """
    if not PIPELINE_WORKLIST.exists():
        return [], {}
    try:
        rows = json.loads(PIPELINE_WORKLIST.read_text()).get("items", [])
    except Exception:
        return [], {}
    order, seen, meta = [], set(), {}
    for r in rows:
        tag_qid = r.get("qid")
        article_qid = r.get("article_qid") or tag_qid
        if article_qid and article_qid not in seen:
            seen.add(article_qid)
            order.append(article_qid)
        if tag_qid:
            meta[tag_qid] = {
                "centrality_pct": r.get("centrality_pct"),
                "brain_rank": r.get("new_rank"),
                "wikilink_rank": r.get("wikilink_rank"),
                "rank_delta": r.get("rank_delta"),
                "priority_source": "brain",
            }
    return order, meta


def ranked_order(cat, offlist=True, order_source="brain"):
    wikilink = list(json.loads(MOST_USED.read_text()).keys())
    if offlist:
        ranked = set(wikilink)
        wikilink += [q for q in cat if q not in ranked]
    if order_source != "brain":
        return wikilink, {}
    brain_order, brain_meta = load_pipeline_worklist()
    if not brain_order:
        return wikilink, {}
    seen = set()
    order = []
    for q in brain_order + wikilink:
        if q not in seen:
            seen.add(q)
            order.append(q)
    return order, brain_meta


def candidates(n=BATCH_SIZE, exclude=(), require_high=True, p31_filter=True, offlist=True,
               order_source="brain"):
    """offlist (ON by default — Jack's call, 2026-07-02): after the most_used
    ranked walk, also walk catalog QIDs that are NOT in the ranking (e.g.
    mathlib 1000.yaml theorem QIDs — maintainer-reviewed but theorem-level, so
    absent from the concept-wikilink ranking), in catalog order. The ranked walk
    still comes FIRST, so batch composition only shifts as the ranked pool
    thins; off-list QIDs multiply the runway. `--no-offlist` restores the
    ranked-only behavior. `order_source='brain'` makes the Brain/control-plane
    worklist steer the head of the order when the artifact exists."""
    cat = load_catalog()
    excl = set(exclude) | seen_qids()           # never re-surface already-reviewed concepts
    if TAGGED.exists():
        excl |= {l.strip() for l in TAGGED.read_text().splitlines() if l.strip().startswith("Q")}
    order, brain_meta = ranked_order(cat, offlist=offlist, order_source=order_source)
    eligible, seen_tag = [], set()
    for q in order:
        if q in excl or q not in cat:
            continue
        c = cat[q]
        if require_high and c.get("confidence") != "high":
            continue
        if not (c.get("file") or "").startswith("Mathlib/"):
            continue                          # untaggable: Lean core / non-library decl
                                              # (Rat, Dvd.dvd, HPow.hPow → Init/… in the toolchain)
        tq = c["tag_qid"]                      # the QID we actually tag (tightest)
        if tq in excl or tq in seen_tag:       # already tagged / in-flight / duplicate concept
            continue
        seen_tag.add(tq)
        row = {"qid": tq, "article_qid": q, "label": c["label"], "decl": c["decl"],
               "file": c["file"], "status": "unreviewed",
               "provenance_tier": c.get("provenance_tier"),
               "source_file": c.get("source_file"),
               "priority_source": "wikilink"}
        if tq in brain_meta:
            row.update({k: v for k, v in brain_meta[tq].items() if v is not None})
            row["review_reason"] = (
                f"Brain/control-plane priority"
                + (f" (centrality {row['centrality_pct']:.2f})"
                   if isinstance(row.get("centrality_pct"), (int, float)) else "")
            )
        eligible.append(row)
    if p31_filter and eligible:
        # only probe the head we might return (bounded network), in rank order
        head = [c["qid"] for c in eligible[: max(n * 3, 60)]]
        fields = field_of_math(head)
        eligible = [c for c in eligible if c["qid"] not in fields]
    return eligible[:n]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=BATCH_SIZE)
    ap.add_argument("--exclude", default="", help="comma-separated qids to skip (in-flight)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-p31", action="store_true", help="skip the Wikidata field-of-math filter (offline)")
    ap.add_argument("--no-offlist", action="store_true",
                    help="ranked-only: exclude catalog QIDs outside the most_used ranking "
                         "(off-list — e.g. 1000.yaml theorems — is the default)")
    ap.add_argument("--order-source", choices=["brain", "wikilink"], default="brain",
                    help="priority order: Brain/control-plane worklist when available, or the old wikilink ranking")
    args = ap.parse_args()
    cands = candidates(args.n, [q.strip() for q in args.exclude.split(",") if q.strip()],
                       p31_filter=not args.no_p31, offlist=not args.no_offlist,
                       order_source=args.order_source)
    if args.json:
        print(json.dumps(cands, indent=1))
    else:
        print(f"{len(cands)} candidate(s):")
        for c in cands:
            print(f"  {c['qid']:12} {c['label'][:34]:34} -> {c['decl']}")
