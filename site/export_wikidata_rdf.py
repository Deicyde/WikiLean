#!/usr/bin/env python3
"""Export WikiLean annotations as Wikidata-aligned RDF + a human concept index.

For each annotated article that has a Wikidata QID, emit triples linking the
Wikidata item to its Mathlib declaration(s) via a custom predicate, plus a
styled, deployable concept index. This is the data backing the Wikidata property
proposal in docs/wikidata_property_proposal.md — the long-term goal is a real
Wikidata property "formalized as (Lean/Mathlib)" so these links live in
Wikidata itself and are queryable via SPARQL.

Outputs:
    out/concepts.html         # styled, deployed concept index (QID + decl links)
    out/wikilean.ttl          # downloadable Turtle dump
    out_w3c/wikilean.ttl      # canonical RDF copy

Usage:
    python export_wikidata_rdf.py     # run after render.py / build_index.py

For private baseline preparation, --d1-articles and --d1-articles-sha256 select
an already verified normalized D1 article object. Its complete, exact annotation
sidecars must also be supplied. Article links then reflect D1 membership, as the
Worker does, without reading rendered article files. This checks correspondence
to the supplied digest; it does not authenticate an acquisition or approve a
public baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ANNOT = ROOT / "annotations"
OUT = ROOT / "out"
OUT_W3C = ROOT / "out_w3c"
CATALOG = ROOT.parent / "catalog" / "data" / "articles.jsonl"

WIKILEAN_NS = "https://wikilean.jackmccarthy.org/ns#"
WD = "http://www.wikidata.org/entity/"
MATHLIB_DOCS = "https://leanprover-community.github.io/mathlib4_docs"

# Keep equal to the Worker's RESERVED set; the dedicated test checks the source.
RESERVED_ARTICLE_SLUGS = frozenset({
    "assets", "api", "favicon.ico", "robots.txt", "sitemap.xml",
    "recent-changes", "flags", "stats", "wikifunctions", "wikifunctions/verify",
    "login", "logout", "graph", "graph_data.json", "article-graph",
    "article-graph-data.json", "review", "queue", "quickstatements", "u",
    "decl", "proposals", "atlas", "brain", "articles", "mcp", "repos", "about",
    "concepts", "map", "map-v2", "map_data_v2.json", "atlas_data.json",
    "wikilean.ttl", "404.html", "brain.html", "concepts.html",
})
MAX_D1_BYTES = 256 * 1024 * 1024
MAX_ARTICLE_BYTES = 16 * 1024 * 1024
MAX_ARTICLES = 100_000


class ExportInputError(ValueError):
    """Explicit baseline inputs do not describe one closed D1 article set."""


def _read_regular(path: Path, limit: int) -> bytes:
    """Read a bounded regular file without following its final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ExportInputError(f"not a bounded regular input: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(descriptor)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size,
                                  item.st_mtime_ns, item.st_ctime_ns)
        if len(raw) > limit or len(raw) != before.st_size or signature(before) != signature(after):
            raise ExportInputError(f"input changed while reading: {path}")
        return raw
    finally:
        os.close(descriptor)


def load_d1_articles(path: Path, expected_sha256: str, annotation_dir: Path) -> list[tuple[str, dict]]:
    """Capture and check exact canonical D1 rows and their complete sidecars."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ExportInputError("D1 article SHA-256 must be 64 lowercase hexadecimal characters")
    # Reuse the actual normalized-row contract, including exact decimal parsing.
    # These imports are intentionally absent from the legacy/default path.
    brain = str(ROOT.parent / "brain")
    if brain not in sys.path:
        sys.path.insert(0, brain)
    import d1_snapshot_bundle as snapshots

    raw = _read_regular(path, MAX_D1_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ExportInputError("D1 article SHA-256 mismatch")
    records = {}
    filenames = set()
    for number, line in enumerate(raw.splitlines(keepends=True), 1):
        if number > MAX_ARTICLES or len(line) > MAX_ARTICLE_BYTES:
            raise ExportInputError("D1 article count or row size exceeds the limit")
        location = f"D1 articles line {number}"
        row = snapshots.contracts.parse_artifact_json_bytes(line, location=location)
        row = snapshots._validate_article(row, location, raw=False)
        canonical = snapshots.contracts.canonical_artifact_json_bytes(row)
        if canonical + b"\n" != line:
            raise ExportInputError(f"{location}: row is not canonical JSONL")
        slug = row["slug"]
        if unicodedata.normalize("NFC", slug) != slug:
            raise ExportInputError(f"{location}: slug must already be Unicode NFC")
        if slug in RESERVED_ARTICLE_SLUGS:
            raise ExportInputError(f"{location}: slug is a reserved Worker route")
        filename_key = snapshots._article_filename_key(slug, location)
        if filename_key in filenames:
            raise ExportInputError(f"{location}: duplicate or colliding article slug")
        filenames.add(filename_key)
        records[slug] = (row, canonical)
    if not records:
        raise ExportInputError("D1 article set must be nonempty")
    if annotation_dir.is_symlink() or not annotation_dir.is_dir():
        raise ExportInputError("annotation input must be a real directory")
    expected_names = {slug + ".json" for slug in records}
    actual_names = {entry.name for entry in annotation_dir.iterdir()}
    if actual_names != expected_names:
        raise ExportInputError("annotation directory does not match the complete D1 article set")
    for slug, (_, canonical) in records.items():
        if _read_regular(annotation_dir / (slug + ".json"), MAX_ARTICLE_BYTES) != canonical:
            raise ExportInputError(f"annotation sidecar differs from its D1 row: {slug}")
    if {entry.name for entry in annotation_dir.iterdir()} != expected_names:
        raise ExportInputError("annotation directory changed while reading")
    # Render the validated captured values, never reopen the sidecars afterward.
    return [(slug, records[slug][0]) for slug in sorted(records)]


def load_qid_map(catalog: Path | None = None, *, strict: bool = False) -> dict:
    """Map article title -> Wikidata QID from the catalog JSONL."""
    qmap = {}
    catalog = CATALOG if catalog is None else catalog
    if not strict and not catalog.exists():
        return qmap
    stream = io.StringIO(_read_regular(catalog, MAX_D1_BYTES).decode("utf-8")) if strict else catalog.open()
    with stream:
        for line in stream:
            try:
                rec = json.loads(line)
            except Exception:
                if strict:
                    raise ExportInputError("catalog contains an invalid JSON row")
                continue
            title = rec.get("title")
            qid = rec.get("wikidata_qid")
            if title and qid:
                qmap[title] = qid
    return qmap


def slug_to_title(slug: str) -> str:
    return slug.replace("_", " ")


def decl_url(module: str | None, decl: str) -> str | None:
    if not module:
        return None
    return f"{MATHLIB_DOCS}/{module.replace('.', '/')}.html#{decl}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d1-articles", type=Path)
    parser.add_argument("--d1-articles-sha256")
    parser.add_argument("--annotations-dir", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--out-w3c-dir", type=Path)
    args = parser.parse_args(argv)
    if (args.d1_articles is None) != (args.d1_articles_sha256 is None):
        parser.error("--d1-articles and --d1-articles-sha256 must be supplied together")
    strict = args.d1_articles is not None
    if strict and any(getattr(args, key) is None for key in ("annotations_dir", "catalog", "out_dir", "out_w3c_dir")):
        parser.error("D1 mode requires explicit --annotations-dir, --catalog, --out-dir and --out-w3c-dir")
    args.annotations_dir = args.annotations_dir or ANNOT
    args.catalog = args.catalog or CATALOG
    args.out_dir = args.out_dir or OUT
    args.out_w3c_dir = args.out_w3c_dir or OUT_W3C
    if strict:
        outputs = (args.out_dir, args.out_w3c_dir)
        inputs = (args.annotations_dir, args.catalog, args.d1_articles)
        for output in outputs:
            if output.is_symlink() or (output.exists() and not output.is_dir()):
                raise ExportInputError("output must be a real directory")
            resolved = output.resolve()
            if any(resolved == path.resolve() or resolved in path.resolve().parents
                   or path.resolve() in resolved.parents for path in inputs):
                raise ExportInputError("output directory overlaps an input")
        left, right = (path.resolve() for path in outputs)
        if left == right or left in right.parents or right in left.parents:
            raise ExportInputError("output directories must not overlap")
        for output, names in ((args.out_dir, ("concepts.html", "wikilean.ttl")),
                              (args.out_w3c_dir, ("wikilean.ttl",))):
            for name in names:
                destination = output / name
                if destination.is_symlink():
                    raise ExportInputError("output file must not be a symlink")
                if destination.exists():
                    info = destination.stat()
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ExportInputError("output file must be regular with no hardlink aliases")
        records = load_d1_articles(args.d1_articles, args.d1_articles_sha256, args.annotations_dir)
    else:
        records = []
        for jf in sorted(args.annotations_dir.glob("*.json")):
            try:
                records.append((jf.stem, json.loads(jf.read_text())))
            except Exception:
                continue
    qmap = load_qid_map(args.catalog, strict=strict)
    out, out_w3c = args.out_dir, args.out_w3c_dir
    out.mkdir(exist_ok=True)
    out_w3c.mkdir(exist_ok=True)

    concepts = []  # (title, slug, qid, [(decl, module)], has_article)
    ttl_lines = [
        f"@prefix wd: <{WD}> .",
        f"@prefix wl: <{WIKILEAN_NS}> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "",
    ]

    n_links = 0
    for slug, d in records:
        title = d.get("wikipedia_title") or slug_to_title(slug)
        qid = qmap.get(title)
        if not qid:
            continue
        decls = []
        seen = set()
        for a in d.get("annotations", []):
            ml = a.get("mathlib") or {}
            decl = ml.get("decl")
            module = ml.get("module")
            if decl and a.get("status") == "formalized" and decl not in seen:
                seen.add(decl)
                decls.append((decl, module))
        if not decls:
            continue
        n_links += 1
        has_article = strict or (out / f"{slug}.html").exists()
        concepts.append((title, slug, qid, decls, has_article))
        subj = f"wd:{qid}"
        for decl, module in decls:
            ttl_lines.append(f'{subj} wl:formalizedAs "{html.escape(decl)}" .')
            url = decl_url(module, decl)
            if url:
                ttl_lines.append(f'{subj} rdfs:seeAlso <{url}> .')

    ttl = "\n".join(ttl_lines) + "\n"
    (out / "wikilean.ttl").write_text(ttl)
    (out_w3c / "wikilean.ttl").write_text(ttl)

    n_decls = sum(len(c[3]) for c in concepts)

    rows = []
    for title, slug, qid, decls, has_article in sorted(concepts, key=lambda c: c[0].lower()):
        t = html.escape(title)
        concept_cell = f'<a href="{slug}.html">{t}</a>' if has_article else t
        decl_cell = ", ".join(
            (f'<a href="{decl_url(module, decl)}"><code>{html.escape(decl)}</code></a>'
             if decl_url(module, decl) else f'<code>{html.escape(decl)}</code>')
            for decl, module in decls
        )
        rows.append(
            f'<tr data-q="{t.lower()} {qid.lower()}">'
            f'<td><a href="https://www.wikidata.org/wiki/{qid}">{qid}</a></td>'
            f'<td>{concept_cell}</td><td>{decl_cell}</td></tr>'
        )

    page = TEMPLATE.format(
        n_links=n_links,
        n_decls=n_decls,
        rows="\n".join(rows),
    )
    (out / "concepts.html").write_text(page)
    print(f"Wrote out/concepts.html + out/wikilean.ttl — "
          f"{n_links} concepts, {n_decls} formalized declarations")


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean — Wikidata concept links</title>
<meta name="description" content="Wikipedia mathematics concepts linked to their Lean/Mathlib formalizations via Wikidata QIDs — the dataset behind a proposed Wikidata property.">
<style>
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#fafbfc; color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.wl-header {{ background:#fff; border-bottom:1px solid #d0d7de; padding:14px 28px;
  display:flex; align-items:center; justify-content:space-between; }}
.wl-brand {{ font-weight:700; color:#0969da; font-size:18px; text-decoration:none; }}
.wl-nav {{ display:flex; gap:18px; }}
.wl-navlink {{ color:#0969da; text-decoration:none; font-size:.9rem; }}
.wl-navlink:hover {{ text-decoration:underline; }}
.wrap {{ max-width:920px; margin:0 auto; padding:32px 28px 64px; }}
h1 {{ font-size:1.7rem; margin:0 0 .5rem; }}
.lead {{ color:#57606a; font-size:1.02rem; line-height:1.6; max-width:720px; }}
.lead a {{ color:#0969da; text-decoration:none; }}
.lead a:hover {{ text-decoration:underline; }}
.stats {{ display:flex; gap:24px; margin:22px 0 6px; flex-wrap:wrap; font-size:.9rem; color:#57606a; }}
.stats b {{ color:#1f2328; }}
.dataset {{ background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:14px 16px;
  margin:18px 0 8px; font-size:.9rem; color:#57606a; }}
.dataset a {{ color:#0969da; text-decoration:none; }}
.dataset code {{ background:#f0f0f0; padding:1px 5px; border-radius:3px; }}
.search {{ width:100%; padding:10px 14px; font-size:1rem; border:1px solid #d0d7de; border-radius:8px;
  margin:18px 0; font-family:inherit; }}
table {{ border-collapse:collapse; width:100%; background:#fff; border:1px solid #d0d7de; border-radius:8px; overflow:hidden; }}
th,td {{ border-bottom:1px solid #eaeef2; padding:8px 12px; text-align:left; font-size:.92rem; vertical-align:top; }}
th {{ background:#f6f8fa; font-size:.8rem; text-transform:uppercase; letter-spacing:.04em; color:#57606a; }}
tr:last-child td {{ border-bottom:none; }}
td a {{ color:#0969da; text-decoration:none; }}
td a:hover {{ text-decoration:underline; }}
code {{ background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:.85em; }}
.empty {{ color:#57606a; padding:24px 0; }}
footer {{ margin-top:40px; padding-top:18px; border-top:1px solid #d0d7de; font-size:.82rem; color:#57606a; }}
footer a {{ color:#0969da; text-decoration:none; }}
</style>
</head>
<body>
<header class="wl-header">
  <a class="wl-brand" href="/">WikiLean</a>
  <nav class="wl-nav">
    <a class="wl-navlink active" href="/concepts">Concepts</a>
    <a class="wl-navlink" href="/brain">Brain</a>
    <a class="wl-navlink" href="/about">About &amp; method</a>
  </nav>
</header>
<div class="wrap">
  <h1>Wikidata concept links</h1>
  <p class="lead">
    Every Wikipedia mathematics concept WikiLean has matched to a
    <em>formalized</em> declaration in
    <a href="https://leanprover-community.github.io/mathlib4_docs/">Mathlib4</a>,
    keyed by its <a href="https://www.wikidata.org/">Wikidata</a> item. This is the
    dataset behind a proposed Wikidata property, <em>&ldquo;formalized as
    (Lean/Mathlib)&rdquo;</em> — the goal is for these links to live in Wikidata
    itself, queryable via SPARQL and maintainable by the community.
    &larr; <a href="/">back to all articles</a>
  </p>
  <div class="stats">
    <span><b>{n_links}</b> concepts</span>
    <span><b>{n_decls}</b> formalized declarations</span>
  </div>
  <div class="dataset">
    Download the dataset as RDF Turtle: <a href="/wikilean.ttl"><code>wikilean.ttl</code></a>
    (predicate <code>wl:formalizedAs</code> + <code>rdfs:seeAlso</code> into the Mathlib docs).
  </div>
  <input class="search" id="q" type="search" placeholder="Filter by concept or QID…" autocomplete="off">
  <table>
    <thead><tr><th>Wikidata</th><th>Concept</th><th>Mathlib declaration(s)</th></tr></thead>
    <tbody id="rows">
{rows}
    </tbody>
  </table>
  <p class="empty" id="empty" style="display:none">No concepts match.</p>
  <footer>
    WikiLean &middot; <a href="https://github.com/Deicyde/WikiLean">source</a> &middot;
    <a href="https://jackmccarthy.org">Jack McCarthy</a>
  </footer>
</div>
<script>
(function(){{
  var q=document.getElementById('q'),
      rows=[].slice.call(document.querySelectorAll('#rows tr')),
      empty=document.getElementById('empty');
  q.addEventListener('input',function(){{
    var t=q.value.trim().toLowerCase(), shown=0;
    rows.forEach(function(r){{
      var hit=r.getAttribute('data-q').indexOf(t)!==-1;
      r.style.display=hit?'':'none'; if(hit)shown++;
    }});
    empty.style.display=shown?'none':'';
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
