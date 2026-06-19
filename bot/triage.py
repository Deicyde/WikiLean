#!/usr/bin/env python3
"""Triage recycled tags with an LLM agent — the ONE non-deterministic step.

For each tag the deterministic settler recycled, ask an LLM (via the `claude`
CLI, headless) to read the reviewer notes + context and decide:
  - "requeue": the concern is a fixable retargeting → add back to the queue,
    with a suggested_decl / fix_hint for the next batch to act on.
  - "cut": the mapping is fundamentally wrong/ambiguous and not worth a slot.

Tag GENERATION stays deterministic elsewhere; this only decides requeue-vs-cut
and proposes a target. Input = settle.py's recycle list (JSON on stdin or --in).
Output = recycle_queue.json (requeued) + cut_log.json.
"""
import argparse, json, subprocess, sys
from pathlib import Path

# Default outputs live in bot/state/ (script-relative), so a caller that omits
# --out-queue/--out-cut (poll.py, daily_bot.py) still writes to the canonical
# location rather than the process CWD. open_batch.py reads state/cut_log.json
# to exclude permanently-cut QIDs from future batches.
STATE = Path(__file__).resolve().parent / "state"

PROMPT = """You are triaging a Wikipedia↔Mathlib `@[wikidata]` cross-reference tag that human \
reviewers did NOT approve for merge into Mathlib. Decide whether it is worth fixing and \
re-reviewing, or cutting.

Wikidata QID: {qid}  (https://www.wikidata.org/wiki/{qid})
Mathlib declaration tagged: {decl}
Mathlib file: {file}
Reviewer verdicts: {verdicts}
Reviewer notes (verbatim):
{notes}

A tag can be wrong in two distinct ways — read the notes to tell which:
  (A) WRONG DECLARATION — the QID/concept is fine but it was attached to the wrong \
declaration (e.g. "tag the class not the structure", "tag `abs` instead"). Fix via \
`suggested_decl`.
  (B) WRONG / TOO-BROAD QID — the declaration is correct but the Wikidata concept is a \
broader parent than what the decl actually formalizes (e.g. reviewer says the decl `Basis` \
should be tagged with Q189569 (Basis) instead of "Coordinate system"; `Module.Dual` with \
Q752487 (Dual space) instead of "Duality"). Fix via `suggested_qid` — and set \
`suggested_decl` to the SAME (correct) declaration so the requeued tag is complete.
Both can apply at once (new qid AND new decl).

REQUEUE if a reviewer note points at a concrete, fixable retargeting of the declaration \
and/or the QID — i.e. the right concept IS in Mathlib/Wikidata, just on a different \
declaration or under a more specific QID. CUT it if the mapping is fundamentally ambiguous, \
the concept isn't cleanly in Mathlib, or it would burn a review slot better spent on a fresh \
high-confidence tag. A requeue MUST change at least one of the declaration or the QID.

Respond with ONLY a JSON object, no prose:
{{"decision": "requeue" | "cut", "reason": "<one sentence>", "suggested_decl": "<Mathlib \
declaration to tag (the corrected one, or the same decl if only the QID changes), or empty>", \
"suggested_qid": "<corrected Wikidata QID like Q189569 if the concept maps to a different/more \
specific QID, or empty to keep {qid}>", "fix_hint": "<short instruction for re-tagging, or empty>"}}"""


def ask_llm(entry, model):
    notes = "\n".join(f"  - {n['login']} [{n['status']}]: {n['text']}" for n in entry.get("notes", [])) or "  (none)"
    prompt = PROMPT.format(qid=entry["qid"], decl=entry.get("decl") or "(unknown)",
                           file=entry.get("file"),
                           verdicts=entry.get("verdicts"), notes=notes)
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return {"decision": "requeue", "reason": "LLM call failed; defaulting to requeue", "_error": out.stderr[:200]}
    try:
        result = json.loads(out.stdout).get("result", out.stdout)
    except json.JSONDecodeError:
        result = out.stdout
    # extract the JSON object from the model's reply
    s, e = result.find("{"), result.rfind("}")
    try:
        return json.loads(result[s:e + 1])
    except Exception:
        return {"decision": "requeue", "reason": "unparseable LLM reply; defaulting to requeue", "_raw": result[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", type=Path, help="settle recycle JSON (else stdin)")
    ap.add_argument("--out-queue", type=Path, default=STATE / "recycle_queue.json")
    ap.add_argument("--out-cut", type=Path, default=STATE / "cut_log.json")
    ap.add_argument("--model", default="")
    ap.add_argument("--dry-run", action="store_true", help="print prompts, don't call the LLM")
    args = ap.parse_args()
    recycle = json.loads(args.inp.read_text() if args.inp else sys.stdin.read())
    if isinstance(recycle, dict):
        recycle = recycle.get("recycle", [])

    requeue, cut = [], []
    for e in recycle:
        if args.dry_run:
            print(f"[dry-run] would triage {e['qid']} with {len(e.get('notes', []))} note(s)")
            continue
        d = ask_llm(e, args.model)
        rec = {**e, "triage": d}
        (requeue if d.get("decision") == "requeue" else cut).append(rec)
        retarget = " ".join(filter(None, [
            f"decl->{d['suggested_decl']}" if d.get("suggested_decl") else "",
            f"qid->{d['suggested_qid']}" if d.get("suggested_qid") else ""]))
        print(f"{e['qid']}: {d.get('decision','?').upper()} — {d.get('reason','')}"
              + (f"  [{retarget}]" if retarget else ""))
    if not args.dry_run:
        args.out_queue.write_text(json.dumps(requeue, indent=1))
        # cut_log is a CUMULATIVE permanent-exclusion ledger (open_batch reads it
        # to keep cut QIDs out of future batches) — merge with any existing cuts
        # rather than overwriting, newest record winning on a QID collision.
        prior = json.loads(args.out_cut.read_text()) if args.out_cut.exists() else []
        merged = {e["qid"]: e for e in prior}
        for e in cut:
            merged[e["qid"]] = e
        args.out_cut.write_text(json.dumps(list(merged.values()), indent=1))
        print(f"\nrequeue {len(requeue)} -> {args.out_queue}   "
              f"cut {len(cut)} this run ({len(merged)} total) -> {args.out_cut}")


if __name__ == "__main__":
    main()
