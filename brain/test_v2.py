#!/usr/bin/env python3
"""Fixture unit tests for build_common's v2 external layer (SCHEMA.md v2).

Exercises, against a tiny synthetic catalog/data/external/ in a tempdir:
minting policy (anchored + 1-hop frontier, per-db cap, frontier ordered by
inbound links), the snippet license guard, links-edge context dedup, concept
projection dedup, page-qid xref minting, the `f` facet bit table, unit
assembly, and the two-file edge writer (write_edges: non-links rows +
full meta → edges.jsonl, links rows + own meta → edges_links.jsonl). No
network, no real catalog inputs — build() itself is covered by the ordinary
rebuild + test_acceptance.py.

Run: python3 brain/test_v2.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_common as bc  # noqa: E402

REG = {
    "nlab": {"url_template": "https://ncatlab.org/nlab/show/{id}",
             "ingest": {"snippets": True}},
    "mathworld": {"url_template": "https://mathworld.wolfram.com/{id}.html",
                  "ingest": {"snippets": False}},
    "stacks": {"url_template": "https://stacks.math.columbia.edu/tag/{id}",
               "ingest": {"snippets": True}},
}

FAILURES: list[str] = []


def check(name: str, cond: bool, note: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {note}" if note and not cond else ""))
    if not cond:
        FAILURES.append(name)


def write_jsonl(path: Path, db: str, rows: list[dict]) -> None:
    with path.open("w") as f:
        f.write(json.dumps({"_meta": {"db": db, "n": len(rows)}}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")


def page(db: str, pid: str, **kw) -> dict:
    return {"db": db, "id": pid, "title": kw.pop("title", pid),
            "url": f"https://example.org/{db}/{pid}", **kw}


def make_external(d: Path) -> None:
    write_jsonl(d / "nlab_pages.jsonl", "nlab", [
        # a: anchored via existing xref edge; carries a licensed snippet
        page("nlab", "a", snippet="idea of a", snippet_license="nLab (attribution)"),
        # b: anchored via CC0 qid -> graph concept Q2
        page("nlab", "b", qid="Q2", kind_hint="definition"),
        page("nlab", "c"),   # frontier: 1 hop from b
        page("nlab", "d"),   # 2 hops (via c) — never minted
        page("nlab", "e"),   # frontier with 2 inbound links (cap priority)
        page("nlab", "f"),   # frontier with 1 inbound link
    ])
    write_jsonl(d / "nlab_links.jsonl", "nlab", [
        {"db": "nlab", "src": "a", "dst": "b", "context": "body"},
        {"db": "nlab", "src": "a", "dst": "b", "context": "statement"},  # dup, better ctx
        {"db": "nlab", "src": "b", "dst": "a", "context": "related"},
        {"db": "nlab", "src": "b", "dst": "c", "context": "body"},
        {"db": "nlab", "src": "c", "dst": "d", "context": "body"},
        {"db": "nlab", "src": "a", "dst": "e", "context": "body"},
        {"db": "nlab", "src": "b", "dst": "e", "context": "body"},
        {"db": "nlab", "src": "b", "dst": "f", "context": "body"},
    ])
    # snippet on a no-content source: the build must strip it (license guard)
    write_jsonl(d / "mathworld_pages.jsonl", "mathworld", [
        page("mathworld", "M1", qid="Q1", snippet="MUST NOT be stored"),
    ])
    # a db with no source_registry key: the whole file is skipped
    write_jsonl(d / "bogusdb_pages.jsonl", "bogusdb", [page("bogusdb", "x")])


def run_layer(d: Path, cap: int = 8000, xref_pairs: set | None = None):
    ext_data = bc.load_external(d, REG)
    return bc.external_layer(
        ext_data,
        concept_qids={"Q1", "Q2"},
        xref_dsts={"xref:nlab:a"},
        concept_anchor={"xref:nlab:a": {"Q1"}},
        xref_pairs=xref_pairs if xref_pairs is not None else {("Q1", "xref:nlab:a")},
        registry=REG, cap=cap)


def test_env_override() -> None:
    old = os.environ.get("BRAIN_EXTERNAL_DIR")
    try:
        os.environ["BRAIN_EXTERNAL_DIR"] = "/nonexistent/ext"
        check("env: BRAIN_EXTERNAL_DIR override",
              bc.external_dir() == Path("/nonexistent/ext"))
        check("env: missing dir loads as no-op",
              bc.load_external(bc.external_dir(), REG) == {})
    finally:
        if old is None:
            os.environ.pop("BRAIN_EXTERNAL_DIR", None)
        else:
            os.environ["BRAIN_EXTERNAL_DIR"] = old


def test_loading(d: Path) -> None:
    ext_data = bc.load_external(d, REG)
    check("load: registered dbs only (bogusdb skipped)",
          sorted(ext_data) == ["mathworld", "nlab"], f"got {sorted(ext_data)}")
    check("load: _meta rows skipped", len(ext_data["nlab"]["pages"]) == 6)


def test_minting(d: Path) -> None:
    ext_nodes, edges, stats = run_layer(d)
    ids = {n["id"] for n in ext_nodes}
    check("mint: anchored via xref dst", "xref:nlab:a" in ids)
    check("mint: anchored via page qid", "xref:nlab:b" in ids)
    check("mint: 1-hop frontier minted", {"xref:nlab:c", "xref:nlab:e",
                                          "xref:nlab:f"} <= ids)
    check("mint: 2-hop page NOT minted", "xref:nlab:d" not in ids)
    check("mint: stats count", stats["minted"] == {"mathworld": 1, "nlab": 5},
          f"got {stats['minted']}")
    b = next(n for n in ext_nodes if n["id"] == "xref:nlab:b")
    check("mint: payload shape", b["type"] == "ext" and b["db"] == "nlab"
          and b["label"] == "b" and b["url"].endswith("/nlab/b")
          and b["qid"] == "Q2" and b["kind_hint"] == "definition")


def test_cap(d: Path) -> None:
    # cap 3: both anchored (a, b) + the frontier page with MOST inbound (e: 2)
    ext_nodes, _edges, stats = run_layer(d, cap=3)
    nlab = {n["id"] for n in ext_nodes if n["db"] == "nlab"}
    check("cap: anchored first, then frontier by inbound",
          nlab == {"xref:nlab:a", "xref:nlab:b", "xref:nlab:e"}, f"got {nlab}")
    check("cap: dropped count recorded", stats["capped"] == {"nlab": 2},
          f"got {stats['capped']}")


def test_snippet_guard(d: Path) -> None:
    ext_nodes, _edges, _stats = run_layer(d)
    a = next(n for n in ext_nodes if n["id"] == "xref:nlab:a")
    m = next(n for n in ext_nodes if n["id"] == "xref:mathworld:M1")
    check("snippet: license-ok db keeps snippet+license",
          a.get("snippet") == "idea of a" and "attribution" in a["snippet_license"])
    check("snippet: no-content db stripped",
          "snippet" not in m and "snippet_license" not in m)


def test_links_edges(d: Path) -> None:
    _n, edges, stats = run_layer(d)
    pl = {(e["src"], e["dst"]): e for e in edges
          if e["kind"] == "links" and not e["evidence"].get("projected")}
    check("links: page edges between minted nodes only",
          ("xref:nlab:c", "xref:nlab:d") not in pl)
    check("links: (src,dst) deduped to best context",
          pl[("xref:nlab:a", "xref:nlab:b")]["evidence"]["context"] == "statement")
    e = pl[("xref:nlab:a", "xref:nlab:b")]
    check("links: provenance shape", e["provenance"]["source"] == "nlab"
          and e["provenance"]["method"] == "internal_link"
          and e["confidence"] == "high")
    check("links: page-edge count", stats["links_page"] == 6,
          f"got {stats['links_page']}")   # a>b b>a b>c a>e b>e b>f


def test_projection(d: Path) -> None:
    _n, edges, stats = run_layer(d)
    proj = [e for e in edges if e["kind"] == "links"
            and e["evidence"].get("projected")]
    pairs = {(e["src"], e["dst"]) for e in proj}
    # a anchors Q1 (xref), b anchors Q2 (qid): a->b projects Q1->Q2 once
    # (two duplicate page links), b->a projects Q2->Q1
    check("proj: both directions, deduped on (src,dst,via)",
          pairs == {("Q1", "Q2"), ("Q2", "Q1")} and len(proj) == 2,
          f"got {sorted(pairs)} ({len(proj)} edges)")
    e = next(e for e in proj if e["src"] == "Q1")
    check("proj: evidence carries via + page pair",
          e["evidence"] == {"projected": True, "via": "nlab",
                            "src_page": "a", "dst_page": "b"}
          and e["confidence"] == "medium")
    check("proj: stats", stats["links_projected"] == 2)


def test_qid_xref(d: Path) -> None:
    _n, edges, stats = run_layer(d)
    qx = {(e["src"], e["dst"]) for e in edges if e["kind"] == "xref"}
    check("qid-xref: minted for qid pages lacking one",
          qx == {("Q2", "xref:nlab:b"), ("Q1", "xref:mathworld:M1")},
          f"got {sorted(qx)}")
    check("qid-xref: stats", stats["xref_from_page_qid"] == 2)
    # a pre-existing pair suppresses the mint
    _n2, edges2, _s2 = run_layer(d, xref_pairs={("Q1", "xref:nlab:a"),
                                                ("Q2", "xref:nlab:b")})
    qx2 = {(e["src"], e["dst"]) for e in edges2 if e["kind"] == "xref"}
    check("qid-xref: existing pair not duplicated",
          qx2 == {("Q1", "xref:mathworld:M1")}, f"got {sorted(qx2)}")


def _edge(src, dst, kind, source, conf="high", ev=None):
    return {"src": src, "dst": dst, "kind": kind,
            "provenance": {"source": source, "method": "t", "pin": "2026-01-01"},
            "confidence": conf, "evidence": ev or {}}


def facet_fixture():
    nodes = [
        {"id": "Q1", "type": "concept", "label": "one", "slug": "One",
         "article_annotations": {"total": 1, "formalized": 1},
         "display": {"status": "formalized"}},
        {"id": "Q2", "type": "concept", "label": "two",
         "display": {"status": "partial"}},
        {"id": "decl:Mathlib:Foo", "type": "decl", "label": "Foo",
         "module": "Mathlib.A.B"},
        {"id": "decl:Mathlib:Bar", "type": "decl", "label": "Bar"},
        {"id": "xref:nlab:a", "type": "ext", "db": "nlab", "label": "a",
         "url": "u", "snippet": "s", "snippet_license": "nLab"},
        {"id": "xref:mathworld:M1", "type": "ext", "db": "mathworld",
         "label": "M1", "url": "u"},
        {"id": "path:Mathlib", "type": "container", "label": "Mathlib"},
    ]
    edges = [
        _edge("Q1", "xref:nlab:a", "xref", "nlab", ev={"value": "a"}),
        _edge("decl:Mathlib:Foo", "xref:stacks:0001", "xref", "stacks",
              ev={"value": "0001"}),
        _edge("Q1", "lit:x", "cites", "theoremgraph"),
        _edge("decl:Mathlib:Bar", "lit:x", "matches", "theoremgraph"),
        _edge("Q1", "decl:Mathlib:Foo", "formalizes", "mathlib",
              ev={"match_kind": "exact", "module": "Mathlib.A.B"}),
        _edge("Q1", "path:Mathlib", "formalizes", "mathlib",
              ev={"match_kind": "field"}, conf="medium"),
    ]
    tag_rows = [{"decl": "Foo", "db": "wikidata", "tag": "Q1"},
                {"decl": "Foo", "db": "stacks", "tag": "0001"},
                {"decl": "Bar", "db": "kerodon", "tag": "000T"}]
    return nodes, edges, tag_rows


def test_facets() -> None:
    nodes, edges, tag_rows = facet_fixture()
    bc.apply_facets(nodes, edges, tag_rows)
    f = {n["id"]: n.get("f", 0) for n in nodes}
    B = bc
    # bits 0-2 PROPAGATE from tagged decls to the concepts they formalize
    # (Foo carries @[wikidata]+@[stacks]) — otherwise the documented filter
    # masks (f=1, f=17) are unsatisfiable on the concept-bearing label index
    check("f: concept bits (xref+formalized+article+lit+nlab+propagated tags)",
          f["Q1"] == B.F_ANY_XREF | B.F_FORMALIZED | B.F_ARTICLE
          | B.F_LITERATURE | B.F_DB_BIT["nlab"]
          | B.F_GOLD_WIKIDATA | B.F_STACKS_ATTR, f"got {f['Q1']}")
    check("f: partial concept", f["Q2"] == B.F_PARTIAL)
    check("f: gold+stacks decl",
          f["decl:Mathlib:Foo"] == B.F_GOLD_WIKIDATA | B.F_STACKS_ATTR
          | B.F_ANY_XREF | B.F_DB_BIT["stacks"], f"got {f['decl:Mathlib:Foo']}")
    check("f: kerodon+matches decl",
          f["decl:Mathlib:Bar"] == B.F_KERODON_ATTR | B.F_LITERATURE)
    check("f: ext with snippet (xref-touched)",
          f["xref:nlab:a"] == B.F_EXT | B.F_DB_BIT["nlab"]
          | B.F_HAS_SNIPPET | B.F_ANY_XREF, f"got {f['xref:nlab:a']}")
    check("f: ext no snippet, no xref touch",
          f["xref:mathworld:M1"] == B.F_EXT | B.F_DB_BIT["mathworld"])
    check("f: zero omitted", "f" not in nodes[-1])


def test_units() -> None:
    nodes, edges, _tags = facet_fixture()
    bc.assemble_units(nodes, edges, {"Q1": "a description"}, REG)
    u = nodes[0]["unit"]
    check("unit: identity + description",
          u["qid"] == "Q1" and u["label"] == "one"
          and u["description"] == "a description")
    check("unit: article from slug+annotations",
          u["article"] == {"slug": "One",
                           "annotations": {"total": 1, "formalized": 1}})
    check("unit: decls from formalizes edges",
          u["decls"] == [{"name": "Foo", "module": "Mathlib.A.B",
                          "match_kind": "exact", "confidence": "high"}],
          f"got {u['decls']}")
    check("unit: containers from formalizes->path",
          u["containers"] == ["path:Mathlib"])
    # the minted ext node's adapter-encoded url wins over the registry
    # template join (ids can carry spaces the template can't encode)
    check("unit: xrefs w/ ext label + ext-node url preferred",
          u["xrefs"] == {"nlab": [{"id": "a", "label": "a", "url": "u"}]},
          f"got {u['xrefs']}")
    u2 = nodes[1]["unit"]
    check("unit: every concept gets one (empty members allowed)",
          u2 == {"qid": "Q2", "label": "two", "decls": [],
                 "containers": [], "xrefs": {}})
    check("unit: non-concepts untouched",
          all("unit" not in n for n in nodes if n["type"] != "concept"))


def test_split_writer() -> None:
    edges = [
        _edge("path:M", "decl:M:X", "contains", "theoremgraph"),
        _edge("Q1", "decl:M:X", "formalizes", "mathlib"),
        _edge("xref:nlab:a", "xref:nlab:b", "links", "nlab",
              ev={"context": "body"}),
        _edge("Q1", "Q2", "links", "nlab",
              ev={"projected": True, "via": "nlab"}),
    ]
    meta = {"schema": "brain/SCHEMA.md",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "counts": {"edges": {"contains": 1, "formalizes": 1, "links": 2}},
            "notes": {"links_page_edges": 1, "links_projected_edges": 1}}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "edges.jsonl"
        out_links = Path(td) / "edges_links.jsonl"
        n = bc.write_edges(edges, meta, out=out, out_links=out_links)
        check("split: row counts returned", n == {"main": 2, "links": 2},
              f"got {n}")
        main_lines = out.read_text().splitlines()
        check("split: main meta is the FULL build meta (byte-compat with the "
              "pre-split file)", json.loads(main_lines[0])["_meta"] == meta)
        check("split: main file excludes links, order preserved",
              [json.loads(l)["kind"] for l in main_lines[1:]]
              == ["contains", "formalizes"])
        links_lines = out_links.read_text().splitlines()
        lm = json.loads(links_lines[0])["_meta"]
        check("split: links meta (own counts + provenance of the split)",
              lm["split_from"] == "edges.jsonl"
              and lm["generated_at"] == meta["generated_at"]
              and lm["counts"] == {"edges": {"links": 2},
                                   "page_level": 1, "projected": 1},
              f"got {lm}")
        rows = [json.loads(l) for l in links_lines[1:]]
        check("split: links file has only links rows, order preserved",
              [r["kind"] for r in rows] == ["links", "links"]
              and rows[0]["src"] == "xref:nlab:a" and rows[1]["src"] == "Q1")
        # zero-links build still (re)writes the file — no stale rows survive
        n0 = bc.write_edges(edges[:2], meta, out=out, out_links=out_links)
        check("split: zero-links build rewrites an empty links file",
              n0 == {"main": 2, "links": 0}
              and len(out_links.read_text().splitlines()) == 1)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_external(d)
        test_env_override()
        test_loading(d)
        test_minting(d)
        test_cap(d)
        test_snippet_guard(d)
        test_links_edges(d)
        test_projection(d)
        test_qid_xref(d)
    test_facets()
    test_units()
    test_split_writer()
    print(f"\n{'FAIL: ' + ', '.join(FAILURES) if FAILURES else 'all green'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
