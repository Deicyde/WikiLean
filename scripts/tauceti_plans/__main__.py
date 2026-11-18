"""python3 -m scripts.tauceti_plans {fetch,build} ... (see docs/BRAIN-PLANS.md)."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m scripts.tauceti_plans")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="acquire and pin the inputs (network)")
    f.add_argument("--inputs", required=True)
    f.add_argument("--pins", help="a PINS.json or PINS.txt: fetch exactly these commits and bytes")
    b = sub.add_parser("build", help="build the sealed export (offline, deterministic)")
    b.add_argument("--inputs", required=True)
    b.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        from .fetch import FetchError, fetch
        try:
            pins = fetch(args.inputs, args.pins)
        except FetchError as e:
            print(f"fetch failed: {e}", file=sys.stderr)
            return 1
        print(json.dumps(pins, indent=1, sort_keys=True))
        return 0
    from .build import BuildError, build
    try:
        manifest = build(args.inputs, args.out)
    except BuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"logical_root": manifest["logical_root"], "files": manifest["files"],
                      "counts": manifest["counts"]}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
