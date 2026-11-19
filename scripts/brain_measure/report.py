#!/usr/bin/env python3
"""Collect both scopes' numbers into one results.json and print an A-against-B table.

    report.py [--out DIR] [--json PATH]

Reads DIR/harvest/facts.json and, per scope, DIR/<scope>/{facts,timings,analysis}.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

JOB = Path(__file__).resolve().parents[3]


def load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def mb(n) -> str:
    return "n/a" if n is None else f"{n / 1e6:,.1f} MB"


def key_rows(scope: dict) -> dict[str, str]:
    """The headline numbers of one scope, as printable strings."""
    f, a, t = scope["facts"], scope["analysis"], scope["timings"]
    s, i = a.get("sizes", {}), a.get("import", {})
    plan = i.get("plan", {})
    proj = i.get("projection_with_name_fts") or i.get("projection") or {}
    q = a.get("queries", {})
    by = s.get("by_table", {})
    return {
        "TauCeti layer decls": f"{f.get('select', {}).get('layer_unique', 0):,}",
        "nodes / edges": f"{f['graph']['nodes']:,} / {f['graph']['edges']:,}",
        "cells / synapses": f"{f['cells']['cells']:,} / {f['cells']['synapses']:,}",
        "serving file": mb(s.get("file_bytes")),
        "cell_pages table": mb(by.get("cell_pages")),
        "names FTS / texts FTS": f"{mb(by.get('names (fts)'))} / {mb(by.get('texts (fts)'))}",
        "import statements": f"{plan.get('statements', 0):,}",
        "statement max / p99": f"{plan.get('max_bytes', 0):,} / {plan.get('p99_bytes', 0):,} B",
        "rows written": f"{i.get('importer_calls', {}).get('rows_written', 0):,}",
        "D1 footprint / x3": f"{mb(proj.get('per_dataset_bytes'))} / {mb(proj.get('x3_bytes'))} "
                             f"({100 * proj.get('x3_share_of_10GB', 0):.1f}% of 10 GB)",
        "8-word OR text (ms)": str(q.get("text_or_8", {}).get("median_ms")),
        "digest recompute (s)": str(a.get("digest", {}).get("open_local_full_check_s")),
        **{f"stage {k} (s / MB)": f"{v['wall_s']} / {v['max_rss_mb']:,}" for k, v in t.items()},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=JOB / "out")
    ap.add_argument("--json", type=Path, default=JOB / "results.json")
    args = ap.parse_args(argv)
    results = {"harvest": load(args.out / "harvest" / "facts.json"), "scopes": {}}
    for scope in ("A", "B"):
        d = args.out / scope
        results["scopes"][scope] = {"facts": load(d / "facts.json"), "timings": load(d / "timings.json"),
                                    "analysis": load(d / "analysis.json")}
    args.json.write_text(json.dumps(results, indent=1) + "\n")
    rows = {s: key_rows(v) for s, v in results["scopes"].items() if v["facts"]}
    keys = list(dict.fromkeys(k for r in rows.values() for k in r))
    print("| | " + " | ".join(rows) + " |\n|---|" + "---|" * len(rows))
    for k in keys:
        print(f"| {k} | " + " | ".join(r.get(k, "") for r in rows.values()) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
