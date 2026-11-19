"""The `fetch` / `build` command line every declaration producer shares."""

from __future__ import annotations

import argparse
import json
import sys


def main(prog: str, argv: list[str] | None = None) -> int:
    """`prog` is the producer's package (`scripts.tauceti_decls`); its `build` and `fetch`
    modules are imported only for the command that runs."""
    import importlib
    ap = argparse.ArgumentParser(prog=f"python3 -m {prog}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="acquire and pin the inputs (network)")
    f.add_argument("--inputs", required=True)
    f.add_argument("--pins", help="a PINS.json or PINS.txt: fetch exactly these commits and bytes")
    b = sub.add_parser("build", help="build the sealed export (offline, deterministic)")
    b.add_argument("--inputs", required=True)
    b.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        from scripts.brain_producers.fetch_decls import FetchError
        fetch = importlib.import_module(f"{prog}.fetch")
        try:
            pins = fetch.fetch(args.inputs, args.pins)
        except FetchError as e:
            print(f"fetch failed: {e}", file=sys.stderr)
            return 1
        print(json.dumps(pins, indent=1, sort_keys=True))
        return 0
    from scripts.brain_producers.decls import BuildError
    build = importlib.import_module(f"{prog}.build")
    try:
        manifest = build.build(args.inputs, args.out)
    except BuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"logical_root": manifest["logical_root"], "files": manifest["files"],
                      "counts": manifest["counts"]}, indent=1, sort_keys=True))
    return 0
