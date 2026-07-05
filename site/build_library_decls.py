#!/usr/bin/env python3
"""Multi-library decl fabric: fetch each enabled library's doc-gen4
declaration index, filter to the library's OWN module roots, and emit one
combined blob for the Worker's /decl/:name resolver (KV `libdecls:v1`).

Every doc-gen4 site serves declarations/declaration-data.bmp — JSON despite
the extension. Each site bundles its full dependency universe (Mathlib, core),
so module_roots filtering is what makes this a per-library index: Cslib 3.2k
own decls out of a 231k-decl site index.

The blob carries each library's docs_base so a library URL churn (the Physlib
double-rename lesson) is fixed by editing catalog/data/libraries.json — no
Worker redeploy. Atomic write; a failed fetch keeps the previous library's
entries from the last good blob when available (per-library fail-soft).

Run: python3 site/build_library_decls.py   (nightly, before the KV push)
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE.parent / "catalog" / "data" / "libraries.json"
OUT = HERE / "out" / "library_decls.json"
UA = "WikiLean-libdecls/1.0 (https://wikilean.jackmccarthy.org)"


def fetch_decls(lib: dict) -> dict[str, str] | None:
    """name → module for the library's own decls, or None on fetch failure."""
    try:
        out = subprocess.run(
            ["curl", "-sS", "-m", "180", "--retry", "2", "-H", f"User-Agent: {UA}",
             lib["declaration_data"]],
            capture_output=True, text=True, timeout=240, check=True).stdout
        decls = json.loads(out)["declarations"]
    except Exception as e:  # noqa: BLE001 — per-library fail-soft
        print(f"  {lib['key']}: FETCH FAILED ({type(e).__name__})", file=sys.stderr)
        return None
    roots = tuple(f"./{r}/" for r in lib["module_roots"])
    own: dict[str, str] = {}
    for name, meta in decls.items():
        link = meta.get("docLink") or ""
        if not link.startswith(roots):
            continue
        # "./Cslib/Foo/Bar.html#Cslib.baz" → module "Cslib.Foo.Bar"
        module = link[2:].split(".html", 1)[0].replace("/", ".")
        own[name] = module
    return own


def main() -> int:
    registry = json.loads(REGISTRY.read_text())["libraries"]
    prev: dict = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text()).get("decls", {})
        except (ValueError, OSError):
            prev = {}
    blob: dict = {"libraries": {}, "decls": {}}
    for lib in registry:
        if not lib.get("enabled"):
            continue
        blob["libraries"][lib["key"]] = {
            "label": lib["label"], "docs_base": lib["docs_base"], "aliases": lib.get("aliases", []),
        }
        own = fetch_decls(lib)
        if own is None:
            own = prev.get(lib["key"], {})  # keep last good on failure
            print(f"  {lib['key']}: keeping {len(own)} decls from the previous blob")
        blob["decls"][lib["key"]] = own
        print(f"  {lib['key']}: {len(own)} own decls")
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False))
    tmp.replace(OUT)
    total = sum(len(v) for v in blob["decls"].values())
    print(f"wrote {OUT.name}: {total} decls across {len(blob['decls'])} libraries "
          f"({OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
