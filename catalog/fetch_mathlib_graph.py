#!/usr/bin/env python3
"""Fetch MathNetwork/MathlibGraph into catalog/.cache/mathnetwork/.

The released dataset of "The Network Structure of Mathlib" (arXiv 2604.24797,
Apache-2.0): the full Mathlib declaration dependency graph with per-edge
`is_explicit` / `is_simplifier` flags — the explicit subgraph is the paper's
proxy for human-intended (non-elaborator-synthesized) dependencies, which
brain/build_rollups.py folds into the tree-grain rollups as w_types.exp.

  edges.csv   (~718 MB)  source,target,is_explicit,is_simplifier (decl names)
  nodes.csv   (~48 MB)   declaration metadata

Mirrors catalog/fetch_math_graph.py (curl; the system python's SSL trust store
is broken on this machine).

Usage: python3 catalog/fetch_mathlib_graph.py [--force]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache" / "mathnetwork"
DATASET = "MathNetwork/MathlibGraph"
FILES = ["edges.csv", "nodes.csv"]
UA = "WikiLean-mathlib-graph-fetch/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)"


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
