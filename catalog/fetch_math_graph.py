#!/usr/bin/env python3
"""Fetch the TheoremGraph math-graph CSVs into catalog/.cache/.

The BRAIN/graph-v2 build path streams two large CSVs that are gitignored and
were previously only ever downloaded by hand (found by the 2026-07-03
self-review: a fresh clone had no way to obtain them):

  statement_formal.csv    (~85 MB)  388k decls across 39 Lean libraries
  formal_dependency.csv   (~1 GB)   11.3M typed decl→decl dependency edges
  slogan.csv              (~1 GB)   NL one-liners per statement (the CC-BY-4.0
                                    slogan source — the theorem-matching set's
                                    license is contested, BRAIN.md:452)

Mirrors catalog/ingest_theorem_graph.py's curl-based download (the system
python's SSL trust store is broken on this machine). CC-BY-4.0 upstream.

Usage: python3 catalog/fetch_math_graph.py [--force]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
DATASET = "uw-math-ai/math-graph"
FILES = ["statement_formal.csv", "formal_dependency.csv", "slogan.csv"]
UA = "WikiLean-math-graph-fetch/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        dst = CACHE / name
        if dst.exists() and not args.force:
            print(f"{name}: present ({dst.stat().st_size / 1e6:.0f} MB) — skipping "
                  f"(--force to refresh)")
            continue
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/main/{name}"
        print(f"downloading {name} from {DATASET} …")
        tmp = dst.with_suffix(".csv.tmp")
        r = subprocess.run(["curl", "-sS", "-L", "-m", "3600", "--retry", "3",
                            "-H", f"User-Agent: {UA}", "-o", str(tmp), url])
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 10 ** 6:
            if tmp.exists():
                tmp.unlink()
            sys.exit(f"FATAL: download failed for {name} (rc={r.returncode})")
        tmp.replace(dst)
        print(f"  -> {dst} ({dst.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
