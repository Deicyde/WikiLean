#!/usr/bin/env python3
"""Emit a pilot subset of articles.jsonl: FA/GA/B + Top/High importance.

This is our v1 high-value slice — the canonical, well-developed math articles
we'll use to validate the Mathlib cross-reference pipeline before scaling.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "data" / "articles.jsonl"
DEFAULT_OUT = HERE / "data" / "pilot.jsonl"

PILOT_CLASSES = {"FA", "GA", "B"}
PILOT_IMPORTANCE = {"Top", "High"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_IN))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    pilot: list[dict] = []
    with open(args.inp) as f:
        for line in f:
            r = json.loads(line)
            if r["class"] in PILOT_CLASSES and r["importance"] in PILOT_IMPORTANCE:
                pilot.append(r)

    with open(args.out, "w", encoding="utf-8") as f:
        for r in pilot:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(pilot)
    n_human = sum(1 for r in pilot if r["is_human"])
    print(f"pilot: {n} articles  →  {args.out}")
    print(f"  human (biographies):  {n_human}")
    print(f"  non-human (concepts): {n - n_human}")
    print()
    print("class × importance (concepts / humans):")
    ct_all = Counter((r["class"], r["importance"]) for r in pilot)
    ct_h = Counter((r["class"], r["importance"]) for r in pilot if r["is_human"])
    for (c, i), total in sorted(ct_all.items()):
        h = ct_h[(c, i)]
        print(f"  {c:3s} × {i:5s}: {total - h:>4} concepts + {h:>3} humans = {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
