#!/usr/bin/env python3
"""Refresh the control plane, then emit a digest.

Runs the three deterministic passes (centrality → coverage → worklists) and
rolls their outputs into manage/data/digest.json — the single artifact that
``/wl-status`` reads and the scheduled agent posts. Optionally mirrors one
already-sealed D1 snapshot to disk first so coverage reflects that exact source.

  python3 manage/refresh.py
  python3 manage/refresh.py --snapshot-bundle /absolute/path/to/<bundle-id>

The optional mirror refresh is offline and consumes one already-acquired,
verified D1 snapshot bundle.  This command never queries or writes D1.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GRAPH = ROOT / "catalog" / "data" / "concept_graph.json"
sys.path.insert(0, str(ROOT / "bot"))
import pool


def _run(cmd: list[str], cwd: Path = ROOT, optional: bool = False) -> bool:
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        msg = f"  ! {cmd[0]} exited {r.returncode}"
        if optional:
            print(msg + " (continuing)")
            return False
        raise SystemExit(msg)
    return True


def refresh(snapshot_bundle: Path | None = None) -> dict:
    if snapshot_bundle is not None:
        snapshot_bundle = Path(snapshot_bundle)
        if not snapshot_bundle.is_absolute():
            raise ValueError("snapshot bundle path must be absolute")
        _run([
            sys.executable,
            str(ROOT / "wiki" / "scripts" / "pull_annotations.py"),
            "--snapshot-bundle",
            str(snapshot_bundle),
        ])
    _run([sys.executable, str(HERE / "centrality.py")])
    _run([sys.executable, str(HERE / "coverage.py")])
    _run([sys.executable, str(HERE / "worklists.py")])

    cov = json.loads((DATA / "coverage.json").read_text())
    cen = json.loads((DATA / "centrality.json").read_text())
    mod = json.loads((DATA / "moderation_worklist.json").read_text())
    pipe = json.loads((DATA / "pipeline_worklist.json").read_text())

    graph_mtime = GRAPH.stat().st_mtime if GRAPH.exists() else 0.0
    ann_mtime = cov.get("annotations_mtime", 0.0)
    n_pool = pipe["total"]

    digest = {
        "generated_at": time.time(),
        "coverage": {**cov["totals"], **cov["backlog"], "n_articles": cov["n_articles"]},
        "graph": {"n_nodes": cen["n_nodes"], "n_edges": cen["n_edges"]},
        "freshness": {
            "annotations_mtime": ann_mtime,
            "graph_mtime": graph_mtime,
            # The concept graph is rebuilt from the catalog, not from D1; if the
            # annotation layer is newer, article-level coverage has moved under it.
            "graph_older_than_annotations": graph_mtime < ann_mtime,
        },
        "pool": {"fresh_candidates": n_pool, "approx_batches": round(n_pool / pool.BATCH_SIZE, 1),
                 "field_qids_excluded": pipe["excluded_fields"]},
        # True totals per queue (never the capped list length), plus a small preview.
        "worklists": {
            "formalize": {"total": mod["formalize"]["total"], "top": mod["formalize"]["items"][:5]},
            "annotate": {"total": mod["annotate"]["total"], "top": mod["annotate"]["items"][:5]},
            "coverage_gaps": {"total": mod["coverage_gaps"]["total"], "top": mod["coverage_gaps"]["items"][:5]},
            "pipeline": {"total": pipe["total"], "top": pipe["items"][:5]},
        },
    }
    (DATA / "digest.json").write_text(json.dumps(digest, ensure_ascii=False, indent=2))
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-bundle",
        type=Path,
        help="absolute sealed D1 bundle to mirror before recomputing",
    )
    parser.add_argument(
        "--pull",
        action="store_true",
        help=(
            "compatibility alias: mirror WIKILEAN_D1_SNAPSHOT_BUNDLE; "
            "never performs a live D1 query"
        ),
    )
    args = parser.parse_args()
    snapshot_bundle = args.snapshot_bundle
    if args.pull:
        if snapshot_bundle is not None:
            parser.error("use either --pull or --snapshot-bundle, not both")
        configured = os.environ.get("WIKILEAN_D1_SNAPSHOT_BUNDLE", "")
        if not configured:
            parser.error(
                "--pull requires an explicit absolute "
                "WIKILEAN_D1_SNAPSHOT_BUNDLE"
            )
        snapshot_bundle = Path(configured)
    if snapshot_bundle is not None and not snapshot_bundle.is_absolute():
        parser.error("snapshot bundle path must be absolute")
    d = refresh(snapshot_bundle=snapshot_bundle)
    c = d["coverage"]
    w = d["worklists"]
    print("\n" + "=" * 60)
    print("WikiLean control-plane digest -> manage/data/digest.json")
    print(f"  coverage : {c['pct_formalized']}% formalized / {c['pct_partial']}% partial "
          f"/ {c['pct_not_formalized']}% not  ({c['n_articles']} articles)")
    print(f"  backlog  : {c['extracted_articles']} articles / {c['extracted_statements']} "
          f"statements awaiting formalization")
    print(f"  worklists: formalize {w['formalize']['total']} · annotate {w['annotate']['total']} "
          f"· coverage-gaps {w['coverage_gaps']['total']} (map)")
    print(f"  pool     : {d['pool']['fresh_candidates']} fresh candidates "
          f"(~{d['pool']['approx_batches']} batches, {d['pool']['field_qids_excluded']} field QIDs excluded)")
    if d["freshness"]["graph_older_than_annotations"]:
        print("  ! concept graph is older than the annotation layer — rebuild it "
              "(catalog/mathlib_deps/merge_graph.py) to refresh centrality")


if __name__ == "__main__":
    main()
