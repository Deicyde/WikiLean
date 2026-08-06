#!/usr/bin/env python3
"""Apply ADJUDICATED decl-existence-sweep verdicts to canonical D1 (human-gated).

Successor to apply_decl_sweep.py's tier system: consumes the per-decl verdicts
produced by the 2026-08-05 three-agent adjudication —
  manage/data/decl_sweep_likely_rename_verdicts.json
  manage/data/decl_sweep_ambiguous_verdicts.json
  manage/data/decl_sweep_nomatch_verdicts.json
joined against manage/data/decl_sweep_proposal.json's citation lists — and
routes every item down exactly one of three paths:

  AUTO      action=rename with confidence verified|high, and action=clear_decl,
            applied to annotations whose LIVE provenance is ai/ai-moderated.
            Echo-verbatim bot save (findLostHuman floor intact, no revid, so
            the Wikipedia pin is untouched).
  PROPOSAL  action=propose_status (proof_wanted overclaims — the adjudicator's
            recommended status, Jack decides), plus ANY fix whose live target
            annotation is provenance 'human'. Delivered as
            meta.ladder.proposals on the same bot save — the Worker merges
            them into moderation_state.proposal (inert until approved,
            deduped vs pending + rejected). A save may carry proposals even
            when it edits nothing.
  REPORT    action=leave and judgment-confidence renames: printed, never
            written anywhere.

DRY-RUN by default (still GETs live D1 so the preview is real); --submit
writes. Idempotent: preconditions (current decl / status / provenance) are
re-checked live per run; anything already fixed or drifted is skipped.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "site"))
sys.path.insert(0, str(REPO / "manage"))
import update_from_upstream as ufu  # noqa: E402
import apply_decl_sweep as ads  # noqa: E402  (get_decl/set_decl/load_token)

DATA = REPO / "manage" / "data"
SWEEP = DATA / "decl_sweep_proposal.json"
VERDICT_FILES = [
    DATA / "decl_sweep_likely_rename_verdicts.json",
    DATA / "decl_sweep_ambiguous_verdicts.json",
    DATA / "decl_sweep_nomatch_verdicts.json",
]
AUTO_CONF = ("verified", "high")
CLEAR_NOTE = " (Cited declaration no longer exists in Mathlib — cleared by the decl-existence sweep.)"


def load_verdicts() -> list[dict]:
    out: list[dict] = []
    for p in VERDICT_FILES:
        v = json.loads(p.read_text())
        rows = v["verdicts"] if isinstance(v, dict) else v
        out.extend(rows)
    return out


def citation_index(sweep: dict) -> dict[str, list[dict]]:
    """old decl -> [{slug, id}] across every sweep category."""
    idx: dict[str, list[dict]] = {}
    for cat in (sweep.get("categories") or {}).values():
        for item in cat:
            for r in item.get("annotations") or []:
                idx.setdefault(item["decl"], []).append({"slug": r["slug"], "id": r["id"]})
    return idx


def proposal_fields_for(a: dict, verdict: dict) -> dict:
    """The whitelisted-field delta for a proposal targeting live annotation `a`.
    applyProposalFields (wiki/src/proposals.ts) replaces whole fields, and
    'decl' alone is not approvable — rename deltas ship the full mathlib obj."""
    if verdict["action"] == "propose_status":
        return {"status": verdict["new_status"]}
    if verdict["action"] == "clear_decl":
        return {"status": verdict.get("new_status") or "not_formalized",
                "note": ((a.get("note") or "") + CLEAR_NOTE)[:2000]}
    # rename
    ml = copy.deepcopy(a.get("mathlib") or {})
    ml["decl"] = verdict["new_decl"]
    return {"mathlib": ml}


def build_plans(sweep: dict, verdicts: list[dict]) -> tuple[dict, dict, list[dict]]:
    """Returns (auto_plan[slug] -> edits, overclaim_props[slug] -> proposals,
    reported). Human-provenance conversion happens later, against live D1."""
    idx = citation_index(sweep)
    auto: dict[str, list[dict]] = {}
    props: dict[str, list[dict]] = {}
    reported: list[dict] = []
    by_decl = {v["old_decl"]: v for v in verdicts}

    for v in verdicts:
        act, conf = v["action"], v.get("confidence")
        if act == "rename" and conf in AUTO_CONF:
            for c in idx.get(v["old_decl"], []):
                auto.setdefault(c["slug"], []).append(
                    {"id": c["id"], "kind": "rename", "expect_decl": v["old_decl"],
                     "new_decl": v["new_decl"], "verdict": v})
        elif act == "clear_decl":
            for c in idx.get(v["old_decl"], []):
                auto.setdefault(c["slug"], []).append(
                    {"id": c["id"], "kind": "clear", "expect_decl": v["old_decl"],
                     "new_status": v.get("new_status") or "not_formalized", "verdict": v})
        elif act in ("leave",) or (act == "rename" and conf == "judgment"):
            reported.append(v)
        # propose_status handled from the overclaim rows below (they carry slug+id)

    seen_over: set[tuple[str, str]] = set()
    for o in sweep.get("overclaims_proof_wanted_badged_formalized", []):
        v = by_decl.get(o["decl"])
        if not v or v["action"] != "propose_status":
            continue
        if (o["slug"], o["id"]) in seen_over:
            continue
        seen_over.add((o["slug"], o["id"]))
        props.setdefault(o["slug"], []).append(
            {"id": o["id"], "expect_decl": o["decl"], "verdict": v})
    return auto, props, reported


def process_slug(s, base: str, token: str, slug: str, edits: list[dict],
                 over_props: list[dict], submit: bool, run_id: str) -> dict:
    art = ufu.get_article(s, base, slug, token)
    if art is None:
        return {"slug": slug, "outcome": "unknown-slug", "applied": 0,
                "proposals": 0, "skipped": len(edits) + len(over_props)}
    anns = art.get("annotations") or []

    def one_pass(live_anns: list[dict]):
        out = copy.deepcopy(live_anns)
        by_id = {a.get("id"): a for a in out if a.get("id")}
        applied, proposals, skipped = [], [], []
        for e in edits:
            a = by_id.get(e["id"])
            if a is None:
                skipped.append((e, "id-not-in-live-D1")); continue
            if ads.get_decl(a) != e["expect_decl"]:
                skipped.append((e, f"decl already {ads.get_decl(a)!r}")); continue
            if a.get("provenance") == "human":
                # never bot-edit a human annotation — convert to a proposal
                proposals.append({
                    "annotationId": e["id"],
                    "fields": proposal_fields_for(a, e["verdict"]),
                    "reason": ("decl-sweep (human-owned): " + e["verdict"]["reason"])[:500]})
                continue
            if a.get("provenance") not in ads.EDITABLE_PROV:
                skipped.append((e, f"provenance={a.get('provenance')!r}")); continue
            if e["kind"] == "rename":
                ads.set_decl(a, e["new_decl"])
            else:  # clear
                a["status"] = e["new_status"]
                if a.get("mathlib") and (a["mathlib"] or {}).get("decl") is not None:
                    a["mathlib"].pop("decl", None)
                a.pop("decl", None)
                note = a.get("note") or ""
                if CLEAR_NOTE.strip() not in note:
                    a["note"] = (note + CLEAR_NOTE)[:2000]
            if a.get("provenance") == "ai":
                a["provenance"] = "ai-moderated"
            applied.append(e)
        for p in over_props:
            a = by_id.get(p["id"])
            if a is None or a.get("status") == "rejected":
                skipped.append((p, "overclaim target gone/tombstoned")); continue
            if ads.get_decl(a) != p["expect_decl"]:
                skipped.append((p, "overclaim decl drifted")); continue
            proposals.append({
                "annotationId": p["id"],
                "fields": proposal_fields_for(a, p["verdict"]),
                "reason": ("proof_wanted overclaim: " + p["verdict"]["reason"])[:500]})
        return out, applied, proposals, skipped

    new_anns, applied, proposals, skipped = one_pass(anns)
    for e, why in skipped:
        print(f"    skip [{e['id']}]: {why}", flush=True)
    for e in applied:
        what = (f"rename {e['expect_decl']} -> {e['new_decl']}" if e["kind"] == "rename"
                else f"clear {e['expect_decl']} -> status {e['new_status']}")
        print(f"    APPLY [{e['id']}] {what}", flush=True)
    for p in proposals:
        print(f"    PROPOSE [{p['annotationId']}] {json.dumps(p['fields'])[:110]}", flush=True)
    if not applied and not proposals:
        return {"slug": slug, "outcome": "no-op", "applied": 0, "proposals": 0,
                "skipped": len(skipped)}
    if not submit:
        return {"slug": slug, "outcome": "dry-run", "applied": len(applied),
                "proposals": len(proposals), "skipped": len(skipped)}

    def payload_for(a_list, version):
        return {"annotations": a_list, "base_version": version,
                "comment": f"decl-sweep-verdicts:{run_id}",
                "meta": {"run_id": run_id, "mode": "decl-sweep-verdicts",
                         "source": "manage/apply_sweep_verdicts.py",
                         "applied": len(applied),
                         "ladder": {"proposals": proposals}}}

    status, body = ufu.post_repin(s, base, token, slug, payload_for(new_anns, art["version"]))
    if status == 409:
        art2 = ufu.get_article(s, base, slug, token)
        if art2 is None:
            return {"slug": slug, "outcome": "409-then-gone", "applied": 0,
                    "proposals": 0, "skipped": len(skipped)}
        new2, applied, proposals, _ = one_pass(art2.get("annotations") or [])
        if not applied and not proposals:
            return {"slug": slug, "outcome": "409-resolved-on-rebase", "applied": 0,
                    "proposals": 0, "skipped": len(edits)}
        status, body = ufu.post_repin(s, base, token, slug, payload_for(new2, art2["version"]))
    if status == 200:
        return {"slug": slug, "outcome": "saved", "applied": len(applied),
                "proposals": len(proposals), "skipped": len(skipped),
                "version": body.get("version")}
    if status == 422:
        return {"slug": slug, "outcome": f"422-human-preservation: {body.get('missing')!r}",
                "applied": 0, "proposals": 0, "skipped": len(skipped)}
    return {"slug": slug, "outcome": f"http-{status}: {str(body.get('error'))[:120]!r}",
            "applied": 0, "proposals": 0, "skipped": len(skipped)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-base", default=ufu.DEFAULT_API_BASE)
    ap.add_argument("--submit", action="store_true", help="actually write (else dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="cap articles (0 = all)")
    args = ap.parse_args()

    sweep = json.loads(SWEEP.read_text())
    verdicts = load_verdicts()
    auto, over_props, reported = build_plans(sweep, verdicts)
    slugs = sorted(set(auto) | set(over_props))
    if args.limit:
        slugs = slugs[:args.limit]
    n_edits = sum(len(v) for v in auto.values())
    n_over = sum(len(v) for v in over_props.values())
    run_id = f"declsweep-verdicts-{sweep.get('generated_at', 0)}"
    mode = "SUBMIT" if args.submit else "DRY-RUN"
    print(f"apply sweep verdicts  {mode}  {len(slugs)} articles / "
          f"{n_edits} auto edits / {n_over} overclaim proposals  -> {args.api_base}")
    print(f"  (report-only verdicts, never written: {len(reported)})")

    token = ads.load_token() if args.submit else "dry-run"
    s = ufu.make_session()
    totals = {"saved": 0, "applied": 0, "proposals": 0, "skipped": 0}
    for i, slug in enumerate(slugs):
        print(f"  [{i + 1}/{len(slugs)}] {slug}", flush=True)
        rec = process_slug(s, args.api_base, token, slug, auto.get(slug, []),
                           over_props.get(slug, []), args.submit, run_id)
        print(f"    -> {rec['outcome']}  (applied {rec['applied']}, "
              f"proposals {rec['proposals']}, skipped {rec['skipped']})", flush=True)
        for k in ("applied", "proposals", "skipped"):
            totals[k] += rec[k]
        if rec["outcome"] == "saved":
            totals["saved"] += 1
        if args.submit and (rec["applied"] or rec["proposals"]):
            time.sleep(ufu.WRITE_PACE_SECONDS)
    print(f"\n{mode} done: {totals['applied']} auto edits + {totals['proposals']} proposals "
          f"across {totals['saved']} written articles; {totals['skipped']} skipped."
          + ("" if args.submit else "  (no writes — pass --submit)"))
    if reported:
        print("\nreport-only (no action taken):")
        for v in reported:
            print(f"  - {v['old_decl']}: {v['action']}/{v.get('confidence')} — {v['reason'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
