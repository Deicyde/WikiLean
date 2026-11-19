"""python3 -m scripts.articles_brain {fetch,build} ... (see docs/ARTICLES-BRAIN.md)."""

from __future__ import annotations

import argparse
import sys

from .canon import dumps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m scripts.articles_brain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="read and pin the live store and overlay (network)")
    f.add_argument("--inputs", required=True)
    f.add_argument("--pins", help="a PINS.json: fetch exactly these bytes, or refuse")
    f.add_argument("--site", default=None, help="the site to read (default: the live site)")
    f.add_argument("--delay", type=float, default=1.0,
                   help="seconds between requests (default 1.0); be polite")
    b = sub.add_parser("build", help="build the sealed export (offline, deterministic)")
    b.add_argument("--inputs", required=True)
    b.add_argument("--out", required=True, help="the export goes in <out>/<logical_root>/")
    b.add_argument("--include-author-identity", action="store_true",
                   help="carry each overlay row's author id. OFF by default: credit is "
                        "Jack's decision (docs/ARTICLES-BRAIN.md)")
    args = ap.parse_args(argv)

    if args.cmd == "fetch":
        from .fetch import SITE, FetchError, fetch
        try:
            pins = fetch(args.inputs, args.pins, site=args.site or SITE, delay=args.delay)
        except FetchError as error:
            print(f"fetch failed: {error}", file=sys.stderr)
            return 1
        print(dumps({"store": pins["store"],
                     "articles": len(pins["inputs"]["articles"]),
                     "overlays": len(pins["inputs"]["overlay"])}))
        return 0

    from .build import BuildError, build
    try:
        target, manifest = build(args.inputs, args.out, args.include_author_identity)
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1
    print(dumps({"export": target, "logical_root": manifest["logical_root"],
                 "files": manifest["files"], "counts": manifest["counts"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
