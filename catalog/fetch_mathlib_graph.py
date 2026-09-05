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

Usage:
  python3 catalog/fetch_mathlib_graph.py --revision <40-hex-commit> [--force]
  python3 catalog/fetch_mathlib_graph.py --revision <commit> --adopt-existing

The revision may instead be supplied through WIKILEAN_MATHLIB_GRAPH_REVISION.
Branches and tags (including main) are rejected.
The adoption mode writes no dataset bytes; it seals a legacy cache only when
all sizes and SHA-256 digests match the reviewed pin registry.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_download import (
    HuggingFaceArtifactError,
    adopt_existing_artifacts,
    fetch_huggingface_artifacts,
    load_reviewed_pin,
    require_reviewed_revision,
    resolve_revision,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache" / "mathnetwork"
DATASET = "MathNetwork/MathlibGraph"
FILES = ["edges.csv", "nodes.csv"]
REVISION_ENV = "WIKILEAN_MATHLIB_GRAPH_REVISION"
UA = "WikiLean-mathlib-graph-fetch/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--force", action="store_true", help="re-download even if present"
    )
    mode.add_argument(
        "--adopt-existing",
        action="store_true",
        help="write sidecars only after existing files match reviewed hashes",
    )
    ap.add_argument(
        "--revision",
        default=os.environ.get(REVISION_ENV),
        help=f"exact 40-hex Hugging Face dataset commit (or {REVISION_ENV})",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pin = load_reviewed_pin(DATASET)
        revision = require_reviewed_revision(
            resolve_revision(
                args.revision, environment_variable=REVISION_ENV
            ),
            pin,
        )
        requests = [
            pin.request(name, CACHE / name)
            for name in FILES
        ]
        if args.adopt_existing:
            results = adopt_existing_artifacts(
                dataset=DATASET,
                revision=revision,
                requests=requests,
            )
        else:
            results = fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=revision,
                requests=requests,
                user_agent=UA,
                force=args.force,
            )
    except HuggingFaceArtifactError as exc:
        raise SystemExit(f"FATAL: {exc}") from exc
    for result in results:
        verb = (
            "downloaded"
            if result.downloaded
            else "adopted/verified"
            if args.adopt_existing
            else "verified"
        )
        size = int(result.metadata["size"])
        print(
            f"{result.destination.name}: {verb} "
            f"({size / 1e6:.0f} MB, revision {revision})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
