#!/usr/bin/env python3
"""Refresh bot/data/tagged_in_master.txt from the LIVE upstream Mathlib master.

Greps every `@[wikidata Q…]` already on leanprover-community/mathlib4:master so
the pool selector never re-proposes a tag that's already merged. Deterministic.

Needs a local mathlib4 git checkout (any branch) to fetch into.
  refresh_tagged.py --mathlib ~/mathlib4 [--dry-run]
"""
import argparse, re, subprocess, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data" / "tagged_in_master.txt"
UPSTREAM = "https://github.com/leanprover-community/mathlib4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mathlib", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    g = ["git", "-C", str(args.mathlib)]

    print(f"fetching {UPSTREAM} master (shallow)…")
    f = subprocess.run(g + ["fetch", "--depth=1", UPSTREAM, "master"], text=True, capture_output=True)
    if f.returncode != 0:
        sys.exit(f"git fetch failed: {f.stderr[:300]}")
    grep = subprocess.run(g + ["grep", "-hoIE", r"wikidata[[:space:]]+Q[0-9]+", "FETCH_HEAD", "--", "Mathlib/"],
                          text=True, capture_output=True)
    qids = sorted(set(re.findall(r"Q\d+", grep.stdout)), key=lambda q: int(q[1:]))
    print(f"found {len(qids)} tagged QIDs on master (was {len(OUT.read_text().split()) if OUT.exists() else 0})")
    if args.dry_run:
        print("[dry-run] not writing.", " ".join(qids[:12]), "…")
        return
    OUT.write_text("\n".join(qids) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
