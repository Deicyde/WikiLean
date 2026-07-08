#!/usr/bin/env python3
"""Publish the tag queue to the wiki's /api/queue (bot bearer token).

Sources (any/all):
  --payload FILE       a ready {"items":[…]} blob (e.g. state/seed_queue.json)
  --recycle FILE       triage output (recycle_queue.json) -> recycled items
  --brain FILE         bot/brain_queue.py output -> Brain-suggested items
  --candidates FILE    pool.py output -> unreviewed items
Auth: --token or $PIPELINE_TOKEN. --dry-run prints the payload instead of POSTing.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path
try:
    from crossref import normalize_tag, spec
except ModuleNotFoundError:  # importable as bot.publish_queue
    from .crossref import normalize_tag, spec

DEV_VARS = Path(__file__).resolve().parent.parent / "wiki" / ".dev.vars"
WD_API = "https://www.wikidata.org/w/api.php"
META_FIELDS = {
    "article_qid", "orig_qid", "orig_id", "concept_qid", "centrality_pct", "brain_rank", "wikilink_rank",
    "rank_delta", "provenance_tier", "source_file", "priority_source",
    "review_reason", "brain_node", "decl_node", "source", "actor_type",
    "added_by", "brain_edge_id", "confidence",
}


def wikidata_labels(qids):
    """Authoritative English label per QID — so the queue shows the label of the
    QID actually being tagged (e.g. Q17295 -> 'Euclidean space'), not a stale
    source-article title. Batched wbgetentities; missing/failed lookups are skipped."""
    labels, qids = {}, [q for q in dict.fromkeys(qids) if q]
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        url = (f"{WD_API}?action=wbgetentities&ids={'|'.join(chunk)}"
               f"&props=labels&languages=en&format=json&origin=*")
        try:
            out = subprocess.run(["curl", "-s", "-H", "User-Agent: WikiLean-bot/1.0", url],
                                 capture_output=True, text=True, timeout=40).stdout
            for q, e in json.loads(out).get("entities", {}).items():
                lab = (e.get("labels", {}).get("en", {}) or {}).get("value")
                if lab:
                    labels[q] = lab
        except Exception:
            pass
    return labels


def find_token():
    """WIKILEAN_API_TOKEN env, else PIPELINE_TOKEN= from wiki/.dev.vars (as moderate.py)."""
    tok = os.environ.get("WIKILEAN_API_TOKEN") or os.environ.get("PIPELINE_TOKEN")
    if tok:
        return tok
    if DEV_VARS.exists():
        for line in DEV_VARS.read_text().splitlines():
            if line.startswith("PIPELINE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def read_items(path):
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        return data.get("items", [])
    return data if isinstance(data, list) else []


def queue_item(src, status, default_db="wikidata"):
    norm = normalize_tag(src, default_db)
    item = {
        "db": norm["db"],
        "id": norm["id"],
        "label": src.get("label"),
        "decl": src.get("decl", ""),
        "file": src.get("file"),
        "status": status,
    }
    if norm["db"] == "wikidata":
        item["qid"] = norm["id"]
    for field in META_FIELDS:
        if field in src and src[field] is not None:
            item[field] = src[field]
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", default="https://wikilean.jackmccarthy.org")
    ap.add_argument("--db", default="wikidata", help="queue database to publish (default: wikidata)")
    ap.add_argument("--payload", type=Path)
    ap.add_argument("--recycle", type=Path)
    ap.add_argument("--brain", type=Path)
    ap.add_argument("--candidates", type=Path)
    ap.add_argument("--exclude", default="", help="comma-separated ids to omit from the published queue")
    ap.add_argument("--token", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sp = spec(args.db)
    token = args.token or find_token()
    exclude = {q.strip() for q in args.exclude.split(",") if q.strip()}

    items = []
    if args.payload and args.payload.exists():
        items += [queue_item(i, i.get("status", "unreviewed"), sp.db) for i in read_items(args.payload)
                  if normalize_tag(i, sp.db)["id"] not in exclude]
    if args.recycle and args.recycle.exists():
        for e in read_items(args.recycle):
            t = e.get("triage", {})
            fix = t.get("fix_hint", "")
            ident = t.get("suggested_id") or t.get("suggested_qid") or e.get("id") or e.get("qid")
            if not ident:
                continue
            if ident in exclude:
                continue
            src = {
                # The corrected concept (what will actually be re-tagged), not the
                # original broad id; keep the original as context.
                "db": sp.db,
                "id": ident,
                "orig_id": e.get("id") or e.get("qid"),
                "decl": t.get("suggested_decl") or e.get("decl") or e.get("current_decl", ""),
                "file": e.get("file"),
                "source": "recycled-review",
                "priority_source": "review-feedback",
                "review_reason": "Reviewer recycle plus triage retarget",
                "notes": e.get("notes", []),
                "retarget": (((t.get("suggested_decl") or "") + (" — " + fix if fix else "")).strip(" —")),
            }
            if sp.db == "wikidata":
                src["qid"] = ident
                src["orig_qid"] = e.get("qid")
            items.append(queue_item(src, "recycled", sp.db))
    if args.brain and args.brain.exists():
        for b in read_items(args.brain):
            ident = normalize_tag(b, sp.db)["id"]
            if ident in exclude:
                continue
            item = queue_item(b, "brain", sp.db)
            item.setdefault("source", "brain-community")
            item.setdefault("priority_source", "brain")
            items.append(item)
    if args.candidates and args.candidates.exists():
        for c in read_items(args.candidates):
            ident = normalize_tag(c, sp.db)["id"]
            if ident in exclude:
                continue
            item = queue_item(c, "unreviewed", sp.db)
            item.setdefault("source", "catalog-pool")
            items.append(item)

    # Stamp Wikidata items with authoritative labels for their tagged QID.
    if sp.db == "wikidata":
        labs = wikidata_labels([it.get("qid") for it in items])
        for it in items:
            it["label"] = labs.get(it.get("qid")) or it.get("label")

    payload = {"items": items}
    if args.dry_run:
        print(json.dumps(payload, indent=1)); return
    if not token:
        sys.exit("no token: set WIKILEAN_API_TOKEN, pass --token, or add PIPELINE_TOKEN= to wiki/.dev.vars")
    endpoint = args.wiki.rstrip("/") + "/api/queue" + ("" if sp.db == "wikidata" else f"/{sp.db}")
    r = subprocess.run(["curl", "-sS", "-X", "POST", endpoint,
                        "-H", "Authorization: Bearer " + token,
                        "-H", "Content-Type: application/json", "-d", json.dumps(payload)],
                       capture_output=True, text=True)
    print(r.stdout or r.stderr)


if __name__ == "__main__":
    main()
