#!/usr/bin/env python3
"""One-off resolver for the 2026-08-06 pending-proposal queue (Jack-run).

Works against the CURRENTLY DEPLOYED Worker (no new endpoints needed):
  1. Applies the owner-ratified proof_wanted policy to the three real
     overclaims on /Conjecture (AI-provenance, bot-editable): status
     formalized -> partial + the clarifying note suffix.
  2. Issues echo bot-saves on the six slugs holding no-delta "confirmation"
     proposals — the Worker's bot-save sweep retires them to `stale` (the
     Conjecture save does the same for its three, which become no-delta the
     moment step 1 lands).

After this, the queue holds only the items that are yours under the current
deployed rules: the Picard-Lindelof rename approve, the two Adjoint_functors
test-artifact rejects, and the one pre-existing nightly proposal.

Idempotent: statuses already partial are skipped; echo saves change nothing;
safe to re-run. Uses the pipeline bearer from wiki/.dev.vars.

Usage:  catalog/.venv/bin/python manage/resolve_pending_20260806.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "site"))
sys.path.insert(0, str(REPO / "manage"))
import update_from_upstream as ufu  # noqa: E402
import apply_decl_sweep as ads  # noqa: E402

# Owner-ratified policy (2026-08-06): proof_wanted stubs badge as 'partial'.
DOWNGRADE = {"4eec7ee18e59", "a824a769c8f8", "f111d3769a84"}  # /Conjecture
SWEEP_SLUGS = [
    "Binomial_distribution", "Abstract_algebra", "Hilberts_problems",
    "7", "Diffeomorphism", "Differentiable_manifold",
]


def main() -> int:
    token = ads.load_token()
    base = ufu.DEFAULT_API_BASE
    s = ufu.make_session()

    art = ufu.get_article(s, base, "Conjecture", token)
    if art is None:
        sys.exit("Conjecture: not found")
    anns = copy.deepcopy(art["annotations"])
    changed = 0
    for a in anns:
        if (a.get("id") in DOWNGRADE and a.get("provenance") != "human"
                and a.get("status") == "formalized"):
            a["status"] = "partial"
            note = a.get("note") or ""
            if ads.PW_NOTE_SUFFIX.strip() not in note:
                a["note"] = (note + ads.PW_NOTE_SUFFIX)[:2000]
            if a.get("provenance") == "ai":
                a["provenance"] = "ai-moderated"
            changed += 1
    if changed:
        st, body = ufu.post_repin(s, base, token, "Conjecture", {
            "annotations": anns, "base_version": art["version"],
            "comment": "proof_wanted policy: formalized -> partial (owner-ratified 2026-08-06)",
            "meta": {"mode": "proof-wanted-policy", "run_id": "pw-policy-20260806",
                     "source": "decl-sweep adjudication", "applied": changed}})
        print(f"Conjecture: {changed} downgraded -> save {st} (v{body.get('version')})")
    else:
        print("Conjecture: nothing to downgrade (already applied) — issuing echo save for the sweep")
        st, _ = ufu.post_repin(s, base, token, "Conjecture", {
            "annotations": art["annotations"], "base_version": art["version"],
            "comment": "no-delta proposal sweep trigger",
            "meta": {"mode": "proposal-sweep", "run_id": "pw-policy-20260806"}})
        print(f"Conjecture: sweep save {st}")

    for slug in SWEEP_SLUGS:
        art = ufu.get_article(s, base, slug, token)
        if art is None:
            print(f"{slug}: NOT FOUND — skipped")
            continue
        st, _ = ufu.post_repin(s, base, token, slug, {
            "annotations": art["annotations"], "base_version": art["version"],
            "comment": "no-delta proposal sweep trigger",
            "meta": {"mode": "proposal-sweep", "run_id": "pw-policy-20260806"}})
        print(f"{slug}: sweep save {st}")
    print("\nDone. Check /stats — pending should be down to your 3 + the 1 nightly item.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
