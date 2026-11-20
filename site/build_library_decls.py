#!/usr/bin/env python3
"""Multi-library decl fabric: fetch each enabled library's declaration index,
filter to the library's OWN module roots, and emit one combined blob for the
Worker's /decl/:name resolver (KV `libdecls:v1`).

A library's `index_format` in catalog/data/libraries.json says what its
`declaration_data` URL serves:

- `doc-gen4` (the default): declarations/declaration-data.bmp, JSON despite the
  extension. Each site bundles its full dependency universe (Mathlib, core), so
  module_roots filtering is what makes this a per-library index: Cslib 3.2k own
  decls out of a 231k-decl site index. The Worker links a decl as
  `docs_base + Module/Path.html#Name`.
- `verso-xref`: a Verso literate site's xref.json, which names each constant's
  page and HTML anchor. Formal Conjectures removed doc-gen4 on 2026-07-29
  (google-deepmind/formal-conjectures#4302) and publishes only this. A Verso
  anchor is a mangled name (`Foo.bar` is `Foo___bar`) on a page per module
  (`Foo/Bar/`), so the blob carries each such decl's link in `hrefs`
  (library → name → page#anchor, relative to docs_base).

The blob carries each library's docs_base so a library URL churn (the Physlib
double-rename lesson) is fixed by editing catalog/data/libraries.json — no
Worker redeploy. Atomic write; a failed fetch keeps the library's entries from
the last good blob when that blob was built from the same index format
(per-library fail-soft).

Run: python3 site/build_library_decls.py   (nightly, before the KV push)
"""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
REGISTRY = HERE.parent / "catalog" / "data" / "libraries.json"
OUT = HERE / "out" / "library_decls.json"
UA = "WikiLean-libdecls/1.0 (https://wikilean.jackmccarthy.org)"

DOCGEN = "doc-gen4"
VERSO = "verso-xref"
FORMATS = (DOCGEN, VERSO)


class FetchError(Exception):
    """A declaration index that could not be fetched or read."""


def fetch_json(url: str) -> object:
    """The JSON document at url.

    --fail turns an HTTP error into curl exit 22, so a 404 page is reported as
    a 404 instead of reaching the JSON parser as HTML.
    """
    try:
        proc = subprocess.run(
            ["curl", "-fsS", "-m", "180", "--retry", "2", "-H", f"User-Agent: {UA}", url],
            capture_output=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FetchError(type(e).__name__) from e
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise FetchError(lines[-1] if lines else f"curl exit {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except ValueError as e:
        raise FetchError(f"not JSON: {type(e).__name__}") from e


def in_roots(module: str, roots: list[str]) -> bool:
    """Whether module is one of roots or under one, on a dot boundary."""
    return any(module == r or module.startswith(r + ".") for r in roots)


def docgen_decls(data: dict, roots: list[str]) -> dict[str, str]:
    """name → module for the library's own decls in a doc-gen4 index."""
    prefixes = tuple(f"./{r}/" for r in roots)
    own: dict[str, str] = {}
    for name, meta in data["declarations"].items():
        link = meta.get("docLink") or ""
        if not link.startswith(prefixes):
            continue
        # "./Cslib/Foo/Bar.html#Cslib.baz" → module "Cslib.Foo.Bar"
        own[name] = link[2:].split(".html", 1)[0].replace("/", ".")
    return own


def verso_decls(data: dict, roots: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """name → module and name → href for the library's own decls in a Verso xref.json.

    An entry reads
    {"address": "/Foo/Bar/", "data": {"module": "Foo.Bar", "private": false}, "id": "Foo___baz"},
    and its href is the address made relative and percent-encoded, then the
    anchor: "Foo/Bar/#Foo___baz". Private constants (`_private.0.…`) are left
    out: their names are not stable, so nothing cites them.
    """
    own: dict[str, str] = {}
    hrefs: dict[str, str] = {}
    for name, entries in data["VersoHtml.constant"]["contents"].items():
        if not entries:
            continue
        entry = entries[0]
        meta = entry.get("data") or {}
        module = meta.get("module") or ""
        address = entry.get("address") or ""
        anchor = entry.get("id") or ""
        if meta.get("private") or not anchor or not in_roots(module, roots):
            continue
        path = address.strip("/")
        if not address.startswith("/") or not path or {".", ".."} & set(path.split("/")):
            continue
        own[name] = module
        hrefs[name] = f"{quote(path, safe='/')}/#{quote(anchor, safe='')}"
    return own, hrefs


def fetch_decls(lib: dict, fetch=fetch_json) -> tuple[dict[str, str], dict[str, str] | None] | None:
    """(name → module, name → href or None) for the library's own decls, or None on failure."""
    fmt = lib.get("index_format", DOCGEN)
    try:
        if fmt not in FORMATS:
            raise FetchError(f"unknown index_format {fmt!r}")
        data = fetch(lib["declaration_data"])
        if fmt == VERSO:
            return verso_decls(data, lib["module_roots"])
        return docgen_decls(data, lib["module_roots"]), None
    except Exception as e:  # noqa: BLE001 — per-library fail-soft
        reason = str(e) if isinstance(e, FetchError) else type(e).__name__
        print(f"  {lib['key']}: FETCH FAILED ({reason})", file=sys.stderr)
        return None


def build_blob(registry: list[dict], prev: dict, fetch=fetch_json) -> dict:
    """The libdecls:v1 blob for the enabled libraries, falling back per library to prev."""
    prev_libs = prev.get("libraries") or {}
    prev_decls = prev.get("decls") or {}
    prev_hrefs = prev.get("hrefs") or {}
    blob: dict = {"libraries": {}, "decls": {}, "hrefs": {}}
    for lib in registry:
        if not lib.get("enabled"):
            continue
        key = lib["key"]
        fmt = lib.get("index_format", DOCGEN)
        blob["libraries"][key] = {
            "label": lib["label"], "docs_base": lib["docs_base"], "aliases": lib.get("aliases", []),
            "index_format": fmt,
        }
        got = fetch_decls(lib, fetch)
        if got is None:
            # Keep last good, but only entries read from the same format: a
            # doc-gen4 entry under a Verso docs_base would link to a page that
            # does not exist, and a Verso entry needs its href.
            same = (prev_libs.get(key) or {}).get("index_format", DOCGEN) == fmt
            if same and (fmt != VERSO or key in prev_hrefs):
                own, hrefs = prev_decls.get(key, {}), prev_hrefs.get(key)
            else:
                own, hrefs = {}, None
            print(f"  {key}: keeping {len(own)} decls from the previous blob")
        else:
            own, hrefs = got
        blob["decls"][key] = own
        if hrefs is not None:
            blob["hrefs"][key] = hrefs
        print(f"  {key}: {len(own)} own decls")
    return blob


def main() -> int:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))["libraries"]
    prev: dict = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
        if not isinstance(prev, dict):
            prev = {}
    blob = build_blob(registry, prev)
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    total = sum(len(v) for v in blob["decls"].values())
    print(f"wrote {OUT.name}: {total} decls across {len(blob['decls'])} libraries "
          f"({OUT.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
