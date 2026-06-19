#!/usr/bin/env python3
"""Bridge harvested reviewer feedback into the batch queues — DETERMINISTIC.

`harvest_corrections.py` writes `bot/data/corrections.jsonl` (explicit, regex-
extracted reviewer feedback). This applies it to the batch queues — no LLM:

  - "correction" records that name a `suggested_qid` (the dominant failure mode:
    right declaration, too-broad QID) → enrich the matching `recycle_queue.json`
    entry with the corrected QID, or ADD a requeue entry if the tag was dropped.
    The requeued tag then carries the SPECIFIC QID the reviewer named, instead of
    repeating the rejected broad one. Maintainer suggestions win on conflict.

  - "addition" records (an approved tag whose note proposes a related tag too) →
    `additions_queue.json` — reviewer-proposed new tags. These still need a decl
    /QID resolution pass before they can enter a batch (the reviewer often names a
    module or a Core decl), so they are queued separately, not auto-injected.

Run after harvest_corrections.py, before the next batch is assembled.
"""
import argparse, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORRECTIONS = HERE / "data" / "corrections.jsonl"
QUEUE = HERE / "state" / "recycle_queue.json"
ADDITIONS = HERE / "state" / "additions_queue.json"
CUTLOG = HERE / "state" / "cut_log.json"


def load_jsonl(path):
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def best_correction_per_qid(corrections):
    """Pick one correction per original qid: maintainer first, then one that
    actually names a suggested_qid, then most recent. Only qid-bearing fixes."""
    by_qid = {}
    for c in corrections:
        if c.get("kind") != "correction" or not c.get("suggested_qid"):
            continue
        q = c["qid"]
        cur = by_qid.get(q)
        if cur is None:
            by_qid[q] = c
            continue
        # prefer maintainer, then keep the first (stable).
        if c.get("is_maintainer") and not cur.get("is_maintainer"):
            by_qid[q] = c
    return by_qid


def apply_corrections(corrections, queue, cut_qids):
    """Enrich existing recycle entries with the corrected QID, or add new ones
    for fixes whose tag was dropped. Returns (queue, n_enriched, n_added)."""
    fixes = best_correction_per_qid(corrections)
    by_qid = {e["qid"]: e for e in queue}
    enriched = added = 0
    for qid, c in fixes.items():
        sug_qid, sug_decl = c.get("suggested_qid"), c.get("suggested_decl")
        if qid in by_qid:
            tr = by_qid[qid].setdefault("triage", {})
            if not tr.get("suggested_qid"):           # don't clobber an existing fix
                tr["suggested_qid"] = sug_qid
                if sug_decl and not tr.get("suggested_decl"):
                    tr["suggested_decl"] = sug_decl
                tr.setdefault("decision", "requeue")
                tr["fix_hint"] = (tr.get("fix_hint") or "") + \
                    f" [corrections.jsonl: use {sug_qid}" + \
                    (f" ({c.get('suggested_qid_label')})" if c.get("suggested_qid_label") else "") + "]"
                enriched += 1
        elif sug_qid not in cut_qids:
            # tag isn't queued (the old triage may have CUT it). The cut was for
            # the original broad qid; the reviewer's fix uses a different qid, so
            # recover it as long as that corrected concept isn't itself cut.
            queue.append({
                "qid": qid, "file": c.get("file"), "decl": c.get("suggested_decl"),
                "from_corrections": True,
                "triage": {
                    "decision": "requeue",
                    "reason": f"reviewer named a more specific QID ({sug_qid})",
                    "suggested_qid": sug_qid,
                    "suggested_decl": sug_decl,
                    "fix_hint": (c.get("note") or "")[:200],
                },
            })
            added += 1
    return queue, enriched, added


def collect_additions(corrections):
    """Reviewer-proposed new tags from approved tags' notes, deduped."""
    out, seen = [], set()
    for c in corrections:
        if c.get("kind") != "addition":
            continue
        key = (c.get("qid"), c.get("suggested_decl"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_qid": c.get("qid"),
            "source_label": c.get("label"),
            "suggested_decl": c.get("suggested_decl"),
            "suggested_qid": c.get("suggested_qid"),
            "note": c.get("note"),
            "reviewer": c.get("reviewer"),
            "is_maintainer": c.get("is_maintainer"),
            "pr": c.get("pr"),
            "status": "unresolved",   # needs decl/QID resolution before batching
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corrections", type=Path, default=CORRECTIONS)
    ap.add_argument("--queue", type=Path, default=QUEUE)
    ap.add_argument("--additions", type=Path, default=ADDITIONS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    corrections = load_jsonl(args.corrections)
    queue = json.loads(args.queue.read_text()) if args.queue.exists() else []
    cut_qids = {e["qid"] for e in (json.loads(CUTLOG.read_text()) if CUTLOG.exists() else [])}

    queue, enriched, added = apply_corrections(corrections, queue, cut_qids)
    additions = collect_additions(corrections)

    print(f"corrections: {sum(1 for c in corrections if c.get('kind')=='correction')} "
          f"({len([c for c in corrections if c.get('suggested_qid')])} with a QID fix)")
    print(f"recycle_queue: {enriched} entries enriched with a corrected QID, "
          f"{added} recovered from cuts -> {len(queue)} total")
    print(f"additions_queue: {len(additions)} reviewer-proposed new tags (unresolved)")
    for e in queue:
        tr = e.get("triage", {})
        if tr.get("suggested_qid"):
            print(f"  fix  {e['qid']} -> {tr.get('suggested_decl')}  @ {tr['suggested_qid']}")
    for a in additions:
        print(f"  add  {a['source_label']} ({a['source_qid']}) -> also tag {a['suggested_decl']}")

    if args.dry_run:
        print("\n[dry-run] nothing written.")
        return
    args.queue.write_text(json.dumps(queue, indent=1, ensure_ascii=False))
    args.additions.write_text(json.dumps(additions, indent=1, ensure_ascii=False))
    print(f"\nwrote {args.queue} and {args.additions}")


if __name__ == "__main__":
    main()
