#!/usr/bin/env python3
"""Apply approved fixes from the decl-existence sweep to canonical D1 (human-gated).

Reads manage/data/decl_sweep_proposal.json and applies ONE tier at a time through
the Worker API (bearer PIPELINE_TOKEN from wiki/.dev.vars). Every annotation is
echoed VERBATIM except the targeted AI ones, so:
  - human annotations are preserved byte-for-byte (findLostHuman stays the floor);
  - the Wikipedia pin is unchanged (no `revid` in the payload = a content edit,
    NOT a re-anchor — preserves the revid-advances-only-on-reanchor invariant).

DRY-RUN by default; pass --submit to write. Idempotent and drift-safe: every edit
re-checks its precondition against LIVE D1 (current decl / status / provenance) and
skips anything already resolved, human-owned, or changed since the sweep ran.

Tiers:
  A  proof_wanted overclaims  -> status formalized/partial => not_formalized
                                 (+ a one-time clarifying note suffix)
  B  likely_rename            -> mathlib.decl => the unique last-segment suggestion
  ambiguous_rename / no_match are review-only and are NEVER applied here.

Usage:
  python3 manage/apply_decl_sweep.py --tier A                # dry-run (default)
  python3 manage/apply_decl_sweep.py --tier A --submit       # write tier A
  python3 manage/apply_decl_sweep.py --tier AB --submit --limit 5
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
import update_from_upstream as ufu  # noqa: E402  (reuse the contract-correct HTTP)

PROPOSAL = REPO / "manage" / "data" / "decl_sweep_proposal.json"
DEV_VARS = REPO / "wiki" / ".dev.vars"
PW_NOTE_SUFFIX = " (Mathlib declares this as `proof_wanted` — stated, not yet proven.)"
EDITABLE_PROV = ("ai", "ai-moderated")  # never touch 'human'


def load_token() -> str:
    if not DEV_VARS.exists():
        sys.exit(f"no {DEV_VARS} — cannot authenticate")
    for line in DEV_VARS.read_text().splitlines():
        if line.startswith("PIPELINE_TOKEN="):
            tok = line.split("=", 1)[1].strip()
            if tok:
                return tok
    sys.exit("PIPELINE_TOKEN not found in wiki/.dev.vars")


def get_decl(a: dict) -> str | None:
    return (a.get("mathlib") or {}).get("decl") or a.get("decl")


def set_decl(a: dict, new: str) -> None:
    if a.get("mathlib") and (a["mathlib"] or {}).get("decl") is not None:
        a["mathlib"]["decl"] = new
    else:
        a["decl"] = new


def build_plan(proposal: dict, tier: str) -> dict[str, list[dict]]:
    """edits[slug] = [ {id, kind, expect_decl, expect_status?, new_decl?/new_status?} ]"""
    plan: dict[str, list[dict]] = {}
    if "A" in tier:
        for o in proposal.get("overclaims_proof_wanted_badged_formalized", []):
            plan.setdefault(o["slug"], []).append({
                "id": o["id"], "kind": "downgrade",
                "expect_decl": o["decl"],
                "expect_status": ("formalized", "partial"),
                "new_status": "not_formalized",
            })
    if "B" in tier:
        for item in proposal.get("categories", {}).get("likely_rename", []):
            new = (item.get("suggested") or [None])[0]
            if not new:
                continue
            for r in item["annotations"]:
                plan.setdefault(r["slug"], []).append({
                    "id": r["id"], "kind": "rename",
                    "expect_decl": item["decl"], "new_decl": new,
                })
    return plan


def apply_edits(annotations: list[dict], edits: list[dict], stamp_moderated: bool
                ) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (new_annotations, applied, skipped). new_annotations is a deep copy
    with the targeted edits applied; all other annotations are echoed verbatim."""
    out = copy.deepcopy(annotations)
    by_id = {a.get("id"): a for a in out if a.get("id")}
    applied: list[dict] = []
    skipped: list[dict] = []
    for e in edits:
        a = by_id.get(e["id"])
        if a is None:
            skipped.append({**e, "why": "id-not-in-live-D1"})
            continue
        prov = a.get("provenance")
        if prov not in EDITABLE_PROV:
            skipped.append({**e, "why": f"provenance={prov} (not editable)"})
            continue
        cur_decl = get_decl(a)
        if cur_decl != e["expect_decl"]:
            skipped.append({**e, "why": f"decl already {cur_decl!r} (expected {e['expect_decl']!r})"})
            continue
        if e["kind"] == "downgrade":
            if a.get("status") not in e["expect_status"]:
                skipped.append({**e, "why": f"status already {a.get('status')!r}"})
                continue
            before = a.get("status")
            a["status"] = e["new_status"]
            note = a.get("note") or ""
            if PW_NOTE_SUFFIX.strip() not in note:
                a["note"] = (note + PW_NOTE_SUFFIX)[:2000]
            applied.append({**e, "before_status": before})
        elif e["kind"] == "rename":
            set_decl(a, e["new_decl"])
            applied.append({**e})
        if stamp_moderated and a.get("provenance") == "ai":
            a["provenance"] = "ai-moderated"
    return out, applied, skipped


def process_slug(s, base: str, token: str, slug: str, edits: list[dict],
                 args, run_id: str) -> dict:
    art = ufu.get_article(s, base, slug, token)
    if art is None:
        return {"slug": slug, "outcome": "unknown-slug", "applied": 0, "skipped": len(edits)}
    annotations = art.get("annotations") or []
    new_anns, applied, skipped = apply_edits(annotations, edits, args.stamp_moderated)
    for sk in skipped:
        print(f"    skip [{sk['id']}] {sk['kind']}: {sk['why']}", flush=True)
    for ap in applied:
        if ap["kind"] == "downgrade":
            print(f"    APPLY [{ap['id']}] downgrade {ap['before_status']}->not_formalized "
                  f"({ap['expect_decl']})", flush=True)
        else:
            print(f"    APPLY [{ap['id']}] rename {ap['expect_decl']} -> {ap['new_decl']}", flush=True)
    if not applied:
        return {"slug": slug, "outcome": "no-op", "applied": 0, "skipped": len(skipped)}
    if not args.submit:
        return {"slug": slug, "outcome": "dry-run", "applied": len(applied), "skipped": len(skipped)}

    payload = {
        "annotations": new_anns,
        "base_version": art["version"],
        "comment": f"decl-sweep:{run_id}",
        "meta": {"run_id": run_id, "mode": "decl-sweep",
                 "source": "manage/decl_existence_sweep.py",
                 "applied": len(applied)},
    }
    status, body = ufu.post_repin(s, base, token, slug, payload)
    if status == 409:
        # someone edited between GET and POST — one rebase: re-GET, re-apply
        # (idempotent recheck skips anything now-resolved), re-POST.
        art2 = ufu.get_article(s, base, slug, token)
        if art2 is None:
            return {"slug": slug, "outcome": "409-then-gone", "applied": 0, "skipped": len(skipped)}
        new2, applied2, _ = apply_edits(art2.get("annotations") or [], edits, args.stamp_moderated)
        if not applied2:
            return {"slug": slug, "outcome": "409-resolved-on-rebase", "applied": 0, "skipped": len(edits)}
        payload["annotations"] = new2
        payload["base_version"] = art2["version"]
        status, body = ufu.post_repin(s, base, token, slug, payload)
    if status == 200:
        return {"slug": slug, "outcome": "saved", "applied": len(applied),
                "skipped": len(skipped), "version": body.get("version")}
    if status == 422:
        return {"slug": slug, "outcome": f"422-human-preservation: {body.get('missing')!r}",
                "applied": 0, "skipped": len(skipped)}
    return {"slug": slug, "outcome": f"http-{status}: {str(body.get('error'))[:120]!r}",
            "applied": 0, "skipped": len(skipped)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proposal", type=Path, default=PROPOSAL)
    ap.add_argument("--api-base", default=ufu.DEFAULT_API_BASE)
    ap.add_argument("--tier", default="A", help="A | B | AB (default A)")
    ap.add_argument("--submit", action="store_true", help="actually write (else dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="cap articles (0 = all)")
    ap.add_argument("--keep-provenance", dest="stamp_moderated", action="store_false",
                    help="do NOT stamp edited 'ai' annotations as 'ai-moderated'")
    ap.set_defaults(stamp_moderated=True)
    args = ap.parse_args()
    tier = args.tier.upper()

    proposal = json.loads(args.proposal.read_text())
    plan = build_plan(proposal, tier)
    slugs = sorted(plan)
    if args.limit:
        slugs = slugs[:args.limit]
    n_edits = sum(len(plan[s]) for s in slugs)
    run_id = f"declsweep-{proposal.get('generated_at', 0)}"
    mode = "SUBMIT" if args.submit else "DRY-RUN"
    print(f"apply decl-sweep  tier={tier}  {mode}  "
          f"{len(slugs)} articles / {n_edits} candidate edits  -> {args.api_base}")
    if args.submit:
        token = load_token()
        s = ufu.make_session()
    else:
        token = "dry-run"
        s = ufu.make_session()

    totals = {"saved": 0, "applied": 0, "skipped": 0, "articles_written": 0}
    for i, slug in enumerate(slugs):
        print(f"  [{i + 1}/{len(slugs)}] {slug}", flush=True)
        # dry-run still GETs live D1 so the precondition preview is real.
        rec = process_slug(s, args.api_base, token, slug, plan[slug], args, run_id)
        print(f"    -> {rec['outcome']}  (applied {rec['applied']}, skipped {rec['skipped']})",
              flush=True)
        totals["applied"] += rec["applied"]
        totals["skipped"] += rec["skipped"]
        if rec["outcome"] == "saved":
            totals["saved"] += 1
            totals["articles_written"] += 1
        if args.submit and rec["applied"]:
            time.sleep(ufu.WRITE_PACE_SECONDS)  # stay under EDIT_LIMITER (30/min)
    print(f"\n{mode} done: {totals['applied']} edits applied across "
          f"{totals['articles_written']} written articles, {totals['skipped']} skipped."
          + ("" if args.submit else "  (no writes — pass --submit)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
