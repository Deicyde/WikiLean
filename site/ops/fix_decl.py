#!/usr/bin/env python3
"""Surgically correct a Mathlib decl on a live WikiLean article (bot save).

For one-off fixes where the agent pipeline would be overkill — e.g. an
annotation whose `mathlib.decl` is mis-namespaced (real theorem, wrong name),
as found by the verify-new-decls quality pass. Dry-run by default; pass --apply
to write.

  # preview the change (no write):
  catalog/.venv/bin/python3 site/ops/fix_decl.py Circle \\
      EuclideanGeometry.IsTangentAt_of_angle_eq_pi_div_two \\
      EuclideanGeometry.Sphere.IsTangentAt_of_angle_eq_pi_div_two

  # apply it:
  ... --apply

Safety: only annotations whose mathlib.decl == <wrong> verbatim are touched;
every other field and annotation (human annotations and tombstones included)
is echoed unchanged, so the server-side findLostHuman assertion still holds.
The save preserves the article's pinned wp revid (echoed back) and rides a
base_version for optimistic concurrency (409 → re-run).
"""
import argparse, json, sys
from pathlib import Path
import requests

API = "https://wikilean.jackmccarthy.org"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "site"))
import native_annotations as native  # noqa: E402


def token() -> str:
    import os
    t = os.environ.get("WIKILEAN_API_TOKEN")
    if t and t.strip():
        return t.strip()
    dev = REPO / "wiki" / ".dev.vars"
    for line in dev.read_text(encoding="utf-8").splitlines():
        if line.startswith("PIPELINE_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("no token: set WIKILEAN_API_TOKEN or PIPELINE_TOKEN in wiki/.dev.vars")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("wrong_decl")
    ap.add_argument("right_decl")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--api", default=API)
    a = ap.parse_args()
    H = {"Authorization": f"Bearer {token()}"}
    base = f"{a.api}/api/article/{a.slug}"

    g = requests.get(base + ".json", headers=H, timeout=30)
    g.raise_for_status()
    art = g.json()
    anns = art["annotations"]
    ver = art["version"]
    revid = art.get("revid")
    hits = [x for x in anns if any(t["decl"] == a.wrong_decl for t in native.mathlib_targets(x))]
    print(f"{a.slug}: version={ver} revid={revid} annotations={len(anns)}")
    print(f"matches for decl {a.wrong_decl!r}: {len(hits)}")
    for x in hits:
        print(f"  id={x.get('id')} prov={x.get('provenance')} status={x.get('status')} "
              f"label={x.get('label')!r}")
    if not hits:
        print("nothing to change.")
        return 0
    if any(x.get("provenance") == "human" or x.get("status") == "rejected" for x in hits):
        print("human-owned items and tombstones require the proposal/editor path", file=sys.stderr)
        return 2
    try:
        replacements = [native.rename_mathlib_target(x, a.wrong_decl, a.right_decl)
                        if native.is_native(x) else ({**x, "mathlib": {**x["mathlib"], "decl": a.right_decl}}
                              if isinstance(x.get("mathlib"), dict) and x["mathlib"].get("decl") is not None
                              else {**x, "decl": a.right_decl})
                        for x in hits]
    except native.NativeAnnotationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for x, changed in zip(hits, replacements):
        x.clear()
        x.update(changed)
    print(f"-> would set decl to {a.right_decl!r} on {len(hits)} annotation(s)")
    if not a.apply:
        print("(dry-run; re-run with --apply to write)")
        return 0

    payload = {"annotations": anns, "base_version": ver,
               "meta": {"mode": "manual-correction",
                        "note": f"decl fix {a.wrong_decl} -> {a.right_decl}"}}
    if isinstance(revid, int):
        payload["revid"] = revid
    p = requests.post(base, headers={**H, "Content-Type": "application/json"},
                      data=json.dumps(payload), timeout=60)
    print(f"POST {p.status_code}: {p.text[:300]}")
    return 0 if p.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
