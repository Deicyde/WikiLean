"""python3 -m scripts.tauceti_plans_ll build ... (see docs/BRAIN-PLANS-LL.md)."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m scripts.tauceti_plans_ll")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="convert a sealed plans export (offline, deterministic)")
    b.add_argument("--export", required=True, help="the plans export (manifest.json and its JSONL files)")
    b.add_argument("--inputs", required=True, help="the export's pinned inputs (TauCetiRoadmap/, trough/, decls/)")
    b.add_argument("--out", required=True, help="a new or empty directory outside every git work tree")
    args = ap.parse_args(argv)
    from .build import BuildError, build
    try:
        manifest = build(args.export, args.inputs, args.out)
    except BuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"logical_root": manifest["logical_root"], "counts": manifest["counts"]},
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
