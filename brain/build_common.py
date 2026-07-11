#!/usr/bin/env python3
"""Shared, deterministic input loading + graph assembly for the BRAIN builders.

build_nodes.py and build_edges.py both call build() and each writes its own
artifact — the node set and the edge set are one joint computation (decl nodes
exist only for decls referenced by >=1 ontology edge), so the assembly lives
here rather than being duplicated or ordered across the two scripts.

Everything is derived from pinned catalog inputs; there is no LLM on this path.
Node/edge shapes are the brain/SCHEMA.md contract. provenance.source values are
keys of catalog/data/source_registry.json (SCHEMA "Provenance & licensing");
the concrete input artifact is named in provenance.method.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "catalog" / "data"
CACHE = ROOT / "catalog" / ".cache"
BRAIN_DATA = HERE / "data"

csv.field_size_limit(10 ** 9)

INPUTS = {
    "concept_graph_v2.json": DATA / "concept_graph_v2.json",
    "rebuild_grounding.json": DATA / "rebuild_grounding.json",
    "hierarchy.json": DATA / "hierarchy.json",
    "wikidata_universe.jsonl": DATA / "wikidata_universe.jsonl",
    "universe_extension.jsonl": DATA / "universe_extension.jsonl",
    "wikidata_crossrefs.json": DATA / "wikidata_crossrefs.json",
    "theoremgraph_links.json": DATA / "theoremgraph_links.json",
    "decl_qid_roles_v2.json": DATA / "decl_qid_roles_v2.json",
    "decl_to_qid_v2.json": DATA / "decl_to_qid_v2.json",
    "wikidata_edges.jsonl": ROOT / "catalog" / "mathlib_deps" / "wikidata_edges.jsonl",
    "theorem_matching.csv": CACHE / "theorem_matching.csv",
    "statement_formal.csv": CACHE / "statement_formal.csv",
}
OPTIONAL_INPUTS = {
    "container_links.jsonl": BRAIN_DATA / "container_links.jsonl",
    "discovery_proposals.jsonl": BRAIN_DATA / "discovery_proposals.jsonl",
    "mathlib_tag_xrefs.jsonl": DATA / "mathlib_tag_xrefs.jsonl",
    "wikidata_descriptions.json": DATA / "wikidata_descriptions.json",
}
REGISTRY = DATA / "source_registry.json"

# The edge set ships as TWO artifacts (GitHub's 100 MB per-file hard limit):
# EDGES_OUT = every kind EXCEPT `links`; EDGES_LINKS_OUT = only kind=='links'
# rows (gitignored, deterministically rebuilt from catalog/data/external/).
# Readers treat a missing EDGES_LINKS_OUT as empty.
EDGES_OUT = BRAIN_DATA / "edges.jsonl"
EDGES_LINKS_OUT = BRAIN_DATA / "edges_links.jsonl"

# "links" sorts last: page-level hyperlinks are the lowest-priority edge kind,
# and appending keeps pre-v2 edge ordering byte-identical.
KIND_ORDER = ["contains", "formalizes", "mentions", "depends", "relates",
              "xref", "cites", "matches", "links"]

# ---- SCHEMA.md v2 facet bitmask `f` -----------------------------------------
F_GOLD_WIKIDATA = 1 << 0    # decl carries a gold @[wikidata] source tag
F_STACKS_ATTR = 1 << 1      # decl carries @[stacks]
F_KERODON_ATTR = 1 << 2     # decl carries @[kerodon]
F_ANY_XREF = 1 << 3         # node is src or dst of >=1 xref edge
F_FORMALIZED = 1 << 4       # concept display.status == formalized
F_PARTIAL = 1 << 5          # concept display.status == partial
F_ARTICLE = 1 << 6          # concept has an annotated WikiLean article
F_LITERATURE = 1 << 7       # node has >=1 cites/matches edge; lit PAPER nodes
                            # (lit:<arxiv_id>, no #ref) carry it natively
F_EXT = 1 << 8              # node is an ext page
F_HAS_SNIPPET = 1 << 15     # ext node stores a licensed content snippet
F_DB_BIT = {"lmfdb_knowl": 1 << 9, "nlab": 1 << 10, "mathworld": 1 << 11,
            "proofwiki": 1 << 12, "stacks": 1 << 13, "oeis": 1 << 14}

# links evidence.context, best-first (dedup keeps the strongest context)
CONTEXT_RANK = {"statement": 0, "proof": 1, "body": 2, "related": 3}

# The xref keys of SCHEMA's edge table (P14534/mathlib is `formalizes` territory,
# kgmid is a KG hub id, not an external DB page — neither becomes an xref edge).
XREF_KEYS = {
    "lmfdb_knowl": "P12987", "nlab": "P4215", "mathworld": "P2812",
    "proofwiki": "P6781", "eom": "P7554", "planetmath": "P7726",
    "oeis": "P829", "metamath": "P12888", "dlmf": "P11497", "msc": "P3285",
}

AFFIRM = {"exact", "inexact"}  # theoremgraph_links _meta.affirm_labels


def _pin(name: str) -> str:
    """ISO date (UTC) of the input file's mtime — the per-edge version pin."""
    return datetime.fromtimestamp(INPUTS.get(name, OPTIONAL_INPUTS.get(name)).stat().st_mtime,
                                  tz=timezone.utc).date().isoformat()


def _majority(counter: Counter) -> str | None:
    if not counter:
        return None
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _lit_id(arxiv_id: str, ref: str) -> str:
    return f"lit:{arxiv_id}#{ref}" if ref else f"lit:{arxiv_id}"


def _edge(src: str, dst: str, kind: str, source: str, method: str, pin: str,
          confidence: str, evidence: dict) -> dict:
    return {"src": src, "dst": dst, "kind": kind,
            "provenance": {"source": source, "method": method, "pin": pin},
            "confidence": confidence, "evidence": evidence}


_MARKUP = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    """Plain-text a title/snippet from an external wiki: drop HTML tags,
    unescape entities, collapse whitespace. Inline $TeX$ passes through."""
    out = html.unescape(_MARKUP.sub(" ", text or ""))
    return re.sub(r"\s+", " ", out).strip()


def _prune(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ---- SCHEMA.md v2: external DB pages → ext nodes / links edges --------------

def external_dir() -> Path:
    """catalog/data/external unless BRAIN_EXTERNAL_DIR overrides (tests)."""
    return Path(os.environ.get("BRAIN_EXTERNAL_DIR", str(DATA / "external")))


def ext_node_cap() -> int:
    return int(os.environ.get("BRAIN_EXT_NODE_CAP", "8000"))


def load_crossref_registry(path: Path | None = None) -> dict[str, dict]:
    """source_registry.json crossref_sources — ext `db` values MUST be keys here."""
    return json.loads((path or REGISTRY).read_text()).get("crossref_sources", {})


def load_external(ext_dir: Path, registry: dict[str, dict]) -> dict[str, dict]:
    """Read brain/ingest output: <db>_pages.jsonl (+ optional <db>_links.jsonl).

    Returns {db: {"pages": [...], "links": [...], "pin": iso-date, "paths": [...]}}
    for every db whose files exist AND whose key is in the crossref registry.
    Missing dir / no files → {} — the whole v2 layer degrades to a no-op.
    """
    out: dict[str, dict] = {}
    if not ext_dir.is_dir():
        return out
    for pp in sorted(ext_dir.glob("*_pages.jsonl")):
        db = pp.name[: -len("_pages.jsonl")]
        if db not in registry:
            print(f"WARNING: {pp.name} has no source_registry crossref_sources "
                  f"key '{db}' — file skipped", file=sys.stderr)
            continue
        pages: list[dict] = []
        with pp.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "_meta" in r:
                    continue
                # contract-violating rows are never minted (fail-soft per row)
                if r.get("db") != db or not r.get("id") or not r.get("title") \
                        or not r.get("url"):
                    continue
                pages.append(r)
        links: list[dict] = []
        lp = ext_dir / f"{db}_links.jsonl"
        if lp.exists():
            with lp.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if "_meta" in r:
                        continue
                    if not r.get("src") or not r.get("dst") or r["src"] == r["dst"]:
                        continue
                    links.append(r)
        pin = datetime.fromtimestamp(pp.stat().st_mtime,
                                     tz=timezone.utc).date().isoformat()
        out[db] = {"pages": pages, "links": links, "pin": pin,
                   "paths": [pp] + ([lp] if lp.exists() else [])}
    # Agent-verified anchors (fold_proposals action:"xref" output): stamp the
    # proposed qid onto pages that have none of their own — the ingest's CC0
    # Wikidata qid always wins over an agent-proposed anchor. Stamped pages
    # feed the same minting/xref/projection logic; qid_source marks the edge
    # provenance as propose-then-approve rather than CC0 fact.
    anchor_path = BRAIN_DATA / "ext_anchor_links.jsonl"
    if anchor_path.exists() and out:
        by_db: dict[str, dict[str, dict]] = {
            db: {str(p["id"]): p for p in rec["pages"]} for db, rec in out.items()}
        n_stamped = 0
        with anchor_path.open() as fh:
            for line in fh:
                r = json.loads(line)
                if "_meta" in r:
                    continue
                page = by_db.get(r.get("db"), {}).get(str(r.get("id")))
                if page is not None and not page.get("qid"):
                    page["qid"] = r["qid"]
                    page["qid_source"] = "ext_anchor"
                    page["qid_confidence"] = r.get("confidence", "medium")
                    n_stamped += 1
        if n_stamped:
            print(f"  ext anchors: {n_stamped} agent-verified page qids stamped "
                  f"(brain/data/ext_anchor_links.jsonl)", file=sys.stderr)
    return out


def external_layer(ext_data: dict[str, dict], *, concept_qids: set[str],
                   xref_dsts: set[str], concept_anchor: dict[str, set],
                   xref_pairs: set[tuple[str, str]], registry: dict[str, dict],
                   cap: int) -> tuple[list[dict], list[dict], dict]:
    """Mint ext nodes + links edges + concept projections per SCHEMA v2.

    Minting policy: anchored pages (the page's `xref:<db>:<id>` is an xref dst
    of some graph node, or its CC0 qid is a graph concept) plus pages <=1
    link-hop from an anchored page, capped per db (anchored first, then the
    frontier by inbound-link count). Snippets are stored ONLY where the
    registry's ingest.snippets says the license permits — enforced here again
    regardless of what the ingest emitted. Returns (ext_nodes, new_edges,
    stats); ext node ids reproduce the historical xref edge dst string
    byte-for-byte so existing xref edges resolve to the new nodes.
    """
    ext_nodes: list[dict] = []
    new_edges: list[dict] = []
    stats = {"minted": {}, "capped": {}, "links_page": 0, "links_projected": 0,
             "xref_from_page_qid": 0, "anchors_outside_graph": 0}
    for db in sorted(ext_data):
        rec = ext_data[db]
        snippets_ok = bool((registry[db].get("ingest") or {}).get("snippets"))
        pin = rec["pin"]
        pages = {p["id"]: p for p in rec["pages"]}

        def eid(pid: str) -> str:
            return f"xref:{db}:{pid}"

        anchored = {pid for pid, p in pages.items()
                    if eid(pid) in xref_dsts or p.get("qid") in concept_qids}
        # fold-verified agent anchors pointing at QIDs the graph doesn't have
        # are legal (they extend the universe) but do nothing here — count
        # them so the drop is visible instead of silent
        stats["anchors_outside_graph"] += sum(
            1 for p in pages.values()
            if p.get("qid_source") == "ext_anchor"
            and p.get("qid") not in concept_qids)
        inbound = Counter(l["dst"] for l in rec["links"])
        frontier: set[str] = set()
        for l in rec["links"]:
            s, d = l["src"], l["dst"]
            if s in anchored and d in pages and d not in anchored:
                frontier.add(d)
            if d in anchored and s in pages and s not in anchored:
                frontier.add(s)
        order = sorted(anchored) + sorted(frontier, key=lambda p: (-inbound[p], p))
        minted_set = set(order[:cap])
        stats["minted"][db] = len(minted_set)
        if len(order) > cap:
            stats["capped"][db] = len(order) - cap

        for pid in sorted(minted_set):
            p = pages[pid]
            # titles/snippets from external wikis can carry raw HTML (Kerodon
            # cite spans, nLab TOC chrome) — strip to plain text here so no
            # markup reaches labels.json or the panel (rendered escaped there:
            # not XSS, but garbled visible tags)
            node = {"id": eid(pid), "type": "ext", "db": db,
                    "label": _strip_markup(p["title"]), "url": p["url"]}
            if snippets_ok and p.get("snippet"):
                node["snippet"] = _strip_markup(p["snippet"])
                node["snippet_license"] = p.get("snippet_license")
            node["kind_hint"] = p.get("kind_hint")
            node["qid"] = p.get("qid")
            if p.get("qid_source"):
                # agent-proposed anchor (fold-verified) — must stay
                # distinguishable from a CC0 ingest qid on the node itself
                node["qid_source"] = p["qid_source"]
            ext_nodes.append(_prune(node))
            # a page whose CC0 qid is a graph concept gets the concept→ext
            # xref edge when no pipeline emitted one (join completeness)
            q = p.get("qid")
            if q in concept_qids and (q, eid(pid)) not in xref_pairs:
                stats["xref_from_page_qid"] += 1
                if p.get("qid_source") == "ext_anchor":
                    method = "sync-agents ext-anchor (fold-verified)"
                    conf = p.get("qid_confidence", "medium")
                else:
                    method, conf = "external-ingest page qid", "high"
                new_edges.append(_edge(q, eid(pid), "xref", db, method, pin,
                                       conf, {"value": pid}))

        # page-level links between MINTED nodes, deduped to the best context
        best: dict[tuple[str, str], str] = {}
        for l in rec["links"]:
            s, d = l["src"], l["dst"]
            if s in minted_set and d in minted_set:
                ctx = l.get("context") or "body"
                cur = best.get((s, d))
                if cur is None or CONTEXT_RANK.get(ctx, 9) < CONTEXT_RANK.get(cur, 9):
                    best[(s, d)] = ctx
        for (s, d), ctx in sorted(best.items()):
            new_edges.append(_edge(eid(s), eid(d), "links", db, "internal_link",
                                   pin, "high", {"context": ctx}))
        stats["links_page"] += len(best)

        # concept projection: page A → page B where both anchor to graph
        # concepts becomes concept→concept, deduped on (src, dst, via=db)
        def anchors(pid: str) -> list[str]:
            qs = set(concept_anchor.get(eid(pid), ()))
            p = pages.get(pid)
            if p and p.get("qid") in concept_qids:
                qs.add(p["qid"])
            return sorted(qs)

        seen_proj: set[tuple[str, str]] = set()
        for l in sorted(rec["links"], key=lambda l: (l["src"], l["dst"])):
            for qa in anchors(l["src"]):
                for qb in anchors(l["dst"]):
                    if qa == qb or (qa, qb) in seen_proj:
                        continue
                    seen_proj.add((qa, qb))
                    new_edges.append(_edge(qa, qb, "links", db,
                                           "internal_link (projected)", pin,
                                           "medium",
                                           {"projected": True, "via": db,
                                            "src_page": l["src"],
                                            "dst_page": l["dst"]}))
        stats["links_projected"] += len(seen_proj)
    return ext_nodes, new_edges, stats


def literature_layer(lit_title: dict[str, str], lic_open: dict[str, bool],
                     cit_path: Path, pin_stmt: str
                     ) -> tuple[list[dict], list[dict], dict]:
    """Mint paper-level literature nodes + containment + bibliography links
    (SCHEMA: `lit:<arxiv_id>` = paper, `lit:<arxiv_id>#<ref>` = statement).

    Papers: one node per distinct arXiv id over the statement ids in
    lit_title. An empty-ref TheoremGraph row already owns the paper id — that
    node IS the paper (same durable key), so nothing new is minted for it.
    `contains`: paper → each ref-bearing statement, mechanically derived from
    the id prefix (statements previously had no parent, so SCHEMA's strict
    single-parent containment holds).
    `links`: paper → paper rows from cit_path (arxiv_citations.jsonl —
    OpenAlex referenced_works, CC0; brain/ingest/openalex_citations.py),
    evidence.context="bibliography", re-filtered to endpoints whose paper
    exists here (defense in depth over the adapter's both-endpoints-ours
    guarantee — a bad row must not dangle). Missing file ⇒ ZERO links edges
    (the citation layer degrades to an exact no-op); papers + contains still
    mint — they derive from the statement layer alone.

    Returns (paper_nodes, edges, stats); deterministic, byte-stable.
    """
    paper_title: dict[str, str] = {}            # paper id -> label
    by_paper: dict[str, list[str]] = defaultdict(list)
    for lid in sorted(lit_title):
        pid = f"lit:{lid[4:].split('#', 1)[0]}"
        paper_title.setdefault(pid, lit_title[lid])
        if lid != pid:
            by_paper[pid].append(lid)
    paper_nodes = [_prune({
        "id": pid, "type": "literature",
        "label": paper_title[pid] or pid,
        "arxiv_id": pid[4:],
        "license_open": lic_open.get(pid[4:]),
    }) for pid in sorted(paper_title) if pid not in lit_title]
    edges: list[dict] = []
    for pid in sorted(by_paper):
        for lid in by_paper[pid]:
            edges.append(_edge(pid, lid, "contains", "theoremgraph",
                               "arxiv-id prefix (paper→statement)", pin_stmt,
                               "high", {"arxiv_id": pid[4:]}))
    n_contains = len(edges)
    n_links = n_dropped = 0
    if cit_path.exists():
        pin_c = datetime.fromtimestamp(cit_path.stat().st_mtime,
                                       tz=timezone.utc).date().isoformat()
        seen: set[tuple[str, str]] = set()
        with cit_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                if "_meta" in r:
                    continue
                s, d = f"lit:{r.get('src')}", f"lit:{r.get('dst')}"
                if (s not in paper_title or d not in paper_title or s == d
                        or (s, d) in seen):
                    n_dropped += 1
                    continue
                seen.add((s, d))
                edges.append(_edge(s, d, "links", "openalex",
                                   "referenced_works", pin_c, "high",
                                   {"context": "bibliography"}))
        n_links = len(seen)
    stats = {"papers": len(paper_title), "papers_new": len(paper_nodes),
             "contains": n_contains, "citations": n_links,
             "citation_rows_dropped": n_dropped}
    return paper_nodes, edges, stats


def assemble_units(nodes: list[dict], edges: list[dict],
                   descriptions: dict[str, str],
                   registry: dict[str, dict]) -> None:
    """Attach the SCHEMA v2 atomic-unit card to every concept node (mutates).

    decls/containers from formalizes edges; xrefs from xref edges (label from
    the minted ext node when present, url from the registry url_template);
    article from slug + article_annotations; description from
    wikidata_descriptions.json (universe description as fallback).
    """
    decl_mod = {n["id"]: n.get("module") for n in nodes if n["type"] == "decl"}
    ext_label = {n["id"]: n.get("label") for n in nodes if n["type"] == "ext"}
    ext_url = {n["id"]: n.get("url") for n in nodes if n["type"] == "ext"}
    fz_decls: dict[str, dict[str, dict]] = defaultdict(dict)
    fz_conts: dict[str, list[str]] = defaultdict(list)
    xrefs: dict[str, dict[str, dict[str, dict]]] = \
        defaultdict(lambda: defaultdict(dict))
    for e in edges:
        src = e["src"]
        if e["kind"] == "formalizes":
            dst = e["dst"]
            if dst.startswith("decl:"):
                bare = dst.split(":", 2)[2]
                entry = _prune({
                    "name": bare,
                    "module": e["evidence"].get("module") or decl_mod.get(dst),
                    "match_kind": e["evidence"].get("match_kind"),
                    "confidence": e["confidence"]})
                cur = fz_decls[src].get(bare)
                if cur is None or (not cur.get("match_kind")
                                   and entry.get("match_kind")):
                    fz_decls[src][bare] = entry
            elif dst.startswith("path:") and dst not in fz_conts[src]:
                fz_conts[src].append(dst)
        elif e["kind"] == "xref" and src.startswith("Q"):
            db = e["provenance"]["source"]
            val = str(e["evidence"].get("value") or e["dst"].split(":", 2)[2])
            tmpl = (registry.get(db) or {}).get("url_template") or ""
            # ids can carry spaces (nlab) — the template join must stay a
            # valid URL; prefer the minted ext node's adapter-encoded url
            url = ext_url.get(e["dst"]) or (
                tmpl.replace("{id}", urllib.parse.quote(val, safe="/:().,'-+~"))
                if tmpl else None)
            xrefs[src][db].setdefault(val, _prune({
                "id": val, "label": ext_label.get(e["dst"]), "url": url}))
    for n in nodes:
        if n["type"] != "concept":
            continue
        qid = n["id"]
        unit: dict = {"qid": qid, "label": n.get("label")}
        desc = descriptions.get(qid) or n.get("description")
        if desc:
            unit["description"] = desc
        if n.get("slug") and n.get("article_annotations"):
            unit["article"] = {"slug": n["slug"],
                               "annotations": n["article_annotations"]}
        unit["decls"] = [fz_decls[qid][k] for k in sorted(fz_decls.get(qid, {}))]
        unit["containers"] = sorted(fz_conts.get(qid, []))
        unit["xrefs"] = {db: [xrefs[qid][db][v] for v in sorted(xrefs[qid][db])]
                         for db in sorted(xrefs.get(qid, {}))}
        n["unit"] = unit


def apply_facets(nodes: list[dict], edges: list[dict],
                 tag_rows: list[dict]) -> None:
    """Set the SCHEMA v2 `f` facet bitmask on every node (mutates; omit at 0)."""
    tag_decls: dict[str, set[str]] = defaultdict(set)
    for r in tag_rows:
        tag_decls[r["db"]].add(r["decl"])
    xref_touch: set[str] = set()
    db_bits: dict[str, int] = defaultdict(int)
    lit: set[str] = set()
    # bits 0-2 PROPAGATE from a tagged decl to the concepts it formalizes —
    # otherwise the bits are decl-only while labels.json/filter enumerate
    # concepts, making the documented masks (f=1, f=17) unsatisfiable
    concept_tag_bits: dict[str, int] = defaultdict(int)
    tagged_all = tag_decls["wikidata"] | tag_decls["stacks"] | tag_decls["kerodon"]
    for e in edges:
        k = e["kind"]
        if k == "xref":
            xref_touch.add(e["src"])
            xref_touch.add(e["dst"])
            db_bits[e["src"]] |= F_DB_BIT.get(e["provenance"]["source"], 0)
        elif k in ("cites", "matches"):
            lit.add(e["src"])
        elif k == "formalizes" and e["dst"].startswith("decl:"):
            bare = e["dst"].split(":", 2)[2]
            if bare in tagged_all:
                bits = 0
                if bare in tag_decls["wikidata"]:
                    bits |= F_GOLD_WIKIDATA
                if bare in tag_decls["stacks"]:
                    bits |= F_STACKS_ATTR
                if bare in tag_decls["kerodon"]:
                    bits |= F_KERODON_ATTR
                concept_tag_bits[e["src"]] |= bits
    for n in nodes:
        f = 0
        t, nid = n["type"], n["id"]
        if t == "decl":
            bare = n["label"]
            if bare in tag_decls["wikidata"]:
                f |= F_GOLD_WIKIDATA
            if bare in tag_decls["stacks"]:
                f |= F_STACKS_ATTR
            if bare in tag_decls["kerodon"]:
                f |= F_KERODON_ATTR
        elif t == "concept":
            st = (n.get("display") or {}).get("status")
            if st == "formalized":
                f |= F_FORMALIZED
            elif st == "partial":
                f |= F_PARTIAL
            if n.get("article_annotations"):
                f |= F_ARTICLE
            f |= concept_tag_bits.get(nid, 0)
        elif t == "ext":
            f |= F_EXT | F_DB_BIT.get(n["db"], 0)
            if n.get("snippet"):
                f |= F_HAS_SNIPPET
        elif t == "literature" and "#" not in nid:
            # paper-level lit nodes (lit:<arxiv_id>) anchor the literature
            # facet natively; statement nodes stay bare
            f |= F_LITERATURE
        if nid in xref_touch:
            f |= F_ANY_XREF
        f |= db_bits.get(nid, 0)
        if nid in lit:
            f |= F_LITERATURE
        if f:
            n["f"] = f


def aggregate_facets(nodes: list[dict], edges: list[dict]) -> None:
    """Set `fa` (subtree-aggregate facet bits) on container nodes (mutates).

    A container "contains" a facet when any decl/sub-container in its contains
    subtree carries it, or when a concept whose dot renders inside it does
    (concepts attach via formalizes → decl-in-subtree or → the container
    itself). Without this, level views can't filter: containers carry no tag
    bits of their own, so a facet chip would dim every folder ("showing 0 of
    N" + a grey canvas — the 2026-07-10 bug report).
    """
    parent = {e["dst"]: e["src"] for e in edges if e["kind"] == "contains"}
    node_f = {n["id"]: n.get("f", 0) for n in nodes}
    fa: dict[str, int] = defaultdict(int)

    def up(start: str | None, bits: int) -> None:
        cur = start
        while cur is not None and bits:
            if fa[cur] & bits == bits:
                return  # ancestors already carry these bits
            fa[cur] |= bits
            cur = parent.get(cur)

    for n in nodes:
        f = n.get("f", 0)
        if f and n["type"] in ("decl", "container"):
            up(parent.get(n["id"]), f)
    for e in edges:
        if e["kind"] != "formalizes":
            continue
        f = node_f.get(e["src"], 0)
        if not f:
            continue
        dst = e["dst"]
        up(parent.get(dst) if dst.startswith("decl:") else dst, f)
    for n in nodes:
        if n["type"] == "container" and fa.get(n["id"]):
            n["fa"] = fa[n["id"]]


def build() -> tuple[list[dict], list[dict], dict]:
    """Returns (nodes, edges, meta) — both lists fully sorted, byte-deterministic."""
    graph = json.loads(INPUTS["concept_graph_v2.json"].read_text())
    grounding = json.loads(INPUTS["rebuild_grounding.json"].read_text())
    hierarchy = json.loads(INPUTS["hierarchy.json"].read_text())
    roles = json.loads(INPUTS["decl_qid_roles_v2.json"].read_text())
    links_doc = json.loads(INPUTS["theoremgraph_links.json"].read_text())
    links, links_meta = links_doc["links"], links_doc["_meta"]

    qids = {n["qid"] for n in graph["nodes"]}

    # ---- decl universe + module/library resolution -------------------------
    # id = decl:<Library>:<FQ name>; the library must be fixed before ANY edge
    # is emitted, so resolution runs over every source first.
    fdecl_qids: dict[str, list[str]] = defaultdict(list)   # formalization role
    mod_votes: dict[str, Counter] = defaultdict(Counter)
    lib_votes: dict[str, Counter] = defaultdict(Counter)
    for n in graph["nodes"]:
        for f in n.get("formalizations") or []:
            fdecl_qids[f["decl"]].append(n["qid"])
            if f.get("module"):
                mod_votes[f["decl"]][f["module"]] += 1
            if f.get("library"):
                lib_votes[f["decl"]][f["library"]] += 1
    fdecls = set(fdecl_qids)
    mention_pairs = sorted((q, d) for d, m in roles.items()
                           for q, r in m.items() if r == "citation")
    ldecls = {l["decl"] for ls in links.values() for l in ls}

    # per-annotation evidence from the article corpus (site/annotations/*.json —
    # the D1 cache): each mentions edge carries how many annotations cite the
    # decl (statuses, labels, deep-linkable ids); each annotated concept gets an
    # article_annotations summary. Articles whose concept is NOT in the graph
    # fall back to the universe slug→QID map and are MINTED as concepts — every
    # annotated article must reach the brain. Fail-soft: no corpus, bare edges.
    ann_dir = ROOT / "site" / "annotations"
    ann_ev: dict[tuple[str, str], dict] = {}
    ann_summary: dict[str, dict] = {}
    ann_new_concepts: set[str] = set()
    ann_extra_pairs: list[tuple[str, str]] = []
    slug2qid_local = {n.get("slug"): n["qid"] for n in graph["nodes"] if n.get("slug")}
    uni_slug2qid: dict[str, str] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        if INPUTS[name].exists():
            with INPUTS[name].open() as fh:
                for line in fh:
                    r = json.loads(line)
                    sl = r.get("enwiki_slug")
                    if sl and r.get("qid"):
                        uni_slug2qid.setdefault(sl, r["qid"])
                        # WikiLean slugs hyphenate en-dashes (Curry–Howard)
                        uni_slug2qid.setdefault(sl.replace("\u2013", "-")
                                                .replace("–", "-"), r["qid"])
    if ann_dir.exists():
        for f in sorted(ann_dir.glob("*.json")):
            if f.name.endswith(".agent1.json"):
                continue
            try:
                doc = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            slug = doc.get("slug")
            anns = doc.get("annotations") or []
            if not slug or not anns:
                continue
            qid = slug2qid_local.get(slug)
            if not qid:
                qid = uni_slug2qid.get(slug)
                if not qid:
                    print(f"WARNING: annotated article {slug} has no QID in the "
                          f"universe — invisible to the brain", file=sys.stderr)
                    continue
                ann_new_concepts.add(qid)
            summ = {"total": len(anns), "formalized": 0, "partial": 0,
                    "not_formalized": 0}
            for a in anns:
                st = a.get("status")
                if st in summ:
                    summ[st] += 1
                dec = (a.get("mathlib") or {}).get("decl")
                if not dec:
                    continue
                if qid in ann_new_concepts:
                    ann_extra_pairs.append((qid, dec))
                ev = ann_ev.setdefault((qid, dec), {
                    "role": "citation", "n_annotations": 0, "statuses": {},
                    "sample": []})
                ev["n_annotations"] += 1
                ev["statuses"][st] = ev["statuses"].get(st, 0) + 1
                if len(ev["sample"]) < 3:
                    ev["sample"].append({"id": a.get("id"),
                                         "label": (a.get("label") or "")[:80],
                                         "status": st})
            ann_summary[qid] = summ
        if ann_new_concepts:
            print(f"  annotated articles outside the graph, minted as concepts: "
                  f"{len(ann_new_concepts)} (+{len(ann_extra_pairs)} mention pairs)",
                  file=sys.stderr)
        mention_pairs = sorted(set(mention_pairs) | set(ann_extra_pairs))
    else:
        print("NOTE: site/annotations missing — mentions edges stay bare",
              file=sys.stderr)
    # @[stacks]/@[kerodon]/@[wikidata] attributes harvested from the mathlib4
    # checkout — loaded before the decl universe is fixed so every gold
    # @[wikidata]-tagged decl becomes a brain node even when no agent pipeline
    # found it independently (27/121 were otherwise absent, per the harvest
    # verifier). Fail-soft: without the harvest the build just loses this layer.
    tag_rows: list[dict] = []
    p = OPTIONAL_INPUTS["mathlib_tag_xrefs.jsonl"]
    if p.exists():
        with p.open() as fh:
            tag_rows = [r for line in fh if line.strip()
                        for r in [json.loads(line)] if "decl" in r]
    else:
        print("NOTE: catalog/data/mathlib_tag_xrefs.jsonl missing — "
              "@[stacks]/@[kerodon] xref edges + @[wikidata] source-tag "
              "provenance skipped", file=sys.stderr)
    source_tagged = {(r["tag"], r["decl"]) for r in tag_rows
                     if r["db"] == "wikidata"}
    # @[stacks]/@[kerodon]-tagged decls join the universe too — without a node
    # the tag-xref edge can't mint, which left the whole Kerodon corpus
    # unanchored (its only join to the brain is these attributes).
    attr_tagged = {r["decl"] for r in tag_rows if r["db"] in ("stacks", "kerodon")}
    decl_set = (set(roles) | fdecls | ldecls | {d for _, d in source_tagged}
                | attr_tagged | {d for _, d in mention_pairs})
    # Annotation citations occasionally carry junk like
    # "MonoidAlgebra.instIsSemisimpleModule (Maschke)" — whitespace is never
    # legal in a Lean identifier, so such names can't resolve anywhere. Drop
    # them (and their mention pairs) rather than mint unreachable decl nodes.
    bad_names = {d for d in decl_set if any(c.isspace() for c in d)}
    if bad_names:
        print(f"WARNING: dropping {len(bad_names)} whitespace-bearing decl "
              f"name(s) from annotation citations: {sorted(bad_names)[:3]}",
              file=sys.stderr)
        decl_set -= bad_names
        mention_pairs = [(q, d) for q, d in mention_pairs if d not in bad_names]

    # grounding evidence text, joined by (qid, decl) — the immutable audit trail
    # (match_kind/status overrides are already applied inside concept_graph_v2).
    grounding_note = {(r["qid"], f["decl"]): f.get("evidence")
                      for r in grounding for f in r.get("formalizations") or []}

    # ---- one streaming pass over theorem_matching.csv ----------------------
    csv_mod: dict[str, str] = {}
    slogans: dict[str, str] = {}
    lic_open: dict[str, bool] = {}          # per paper (arxiv_id)
    lit_title: dict[str, str] = {}          # per lit id
    lit_sids: dict[str, dict] = {}          # TheoremGraph UUIDs = session keys only
    match_rows: list[dict] = []             # both-judges-affirmed, grounded decls
    with INPUTS["theorem_matching.csv"].open(newline="") as fh:
        for row in csv.DictReader(fh):
            d = row["formal_decl"]
            if d in decl_set:
                if row["formal_module"] and d not in csv_mod:
                    csv_mod[d] = row["formal_module"]
            if row["arxiv_id"] and row["arxiv_id"] not in lic_open:
                lic_open[row["arxiv_id"]] = row["license_open"] == "True"
            if (row["gpt54_label"] in AFFIRM and row["deepseek_label"] in AFFIRM
                    and d in fdecls):
                lid = _lit_id(row["arxiv_id"], row["informal_ref"])
                lit_title.setdefault(lid, row["paper_title"])
                lit_sids.setdefault(lid, {"query_sid": row["query_sid"],
                                          "cand_sid": row["cand_sid"]})
                match_rows.append({
                    "decl": d, "lit": lid, "arxiv_id": row["arxiv_id"],
                    "ref": row["informal_ref"], "title": row["paper_title"],
                    "sim": float(row["sim"]), "gpt54": row["gpt54_label"],
                    "deepseek": row["deepseek_label"],
                })

    # statement_formal.csv: module backstop for decls the matching sample never
    # saw, plus kind + docstring (the snapshot's `body` column is empty, so the
    # code itself comes from the live checkout below)
    unresolved = {d for d in decl_set if d not in mod_votes and d not in csv_mod}
    sf_mod: dict[str, str] = {}
    decl_code: dict[str, dict] = {}
    sid2decl: dict[str, str] = {}
    with INPUTS["statement_formal.csv"].open(newline="") as fh:
        for row in csv.DictReader(fh):
            d = row["decl_name"]
            if d in decl_set and row.get("statement_id"):
                sid2decl.setdefault(row["statement_id"], d)
            if d in unresolved and row["module"] and d not in sf_mod:
                sf_mod[d] = row["module"]
            if d in decl_set and d not in decl_code:
                rec = _prune({
                    "decl_kind": row.get("kind") or None,
                    "docstring": (row.get("docstring") or "")[:280] or None,
                })
                if rec:
                    decl_code[d] = rec

    # decl slogans from math-graph slogan.csv (CC-BY-4.0) — NOT from
    # theorem_matching.csv's formal_slogan: that dataset's license is
    # contested upstream (CC-BY-SA card vs CC-BY-NC-SA paper, BRAIN.md:452),
    # so it stays link-facts-only. Fail-soft: no file, no slogans.
    slogan_csv = CACHE / "slogan.csv"
    if slogan_csv.exists():
        with slogan_csv.open(newline="") as fh:
            for row in csv.DictReader(fh):
                d = sid2decl.get(row["statement_id"])
                if (d and d not in slogans and row.get("slogan")
                        and row.get("insufficient_context") != "True"):
                    slogans[d] = row["slogan"][:500]
        print(f"  slogans from slogan.csv (CC-BY-4.0): {len(slogans)}/{len(decl_set)} decls",
              file=sys.stderr)
    else:
        print(f"NOTE: {slogan_csv} missing — decl slogans skipped "
              f"(catalog/fetch_math_graph.py)", file=sys.stderr)

    # ---- containers from hierarchy.json ------------------------------------
    lib_meta = hierarchy["libraries"]
    containers: dict[str, dict] = {}
    contains_edges: list[dict] = []
    pin_h = _pin("hierarchy.json")
    snapshot_pin = hierarchy["meta"]["source_sha256"]

    def walk(lib: str, kind: str, name: str, node: dict, parent: str, inherited: bool):
        cid = f"{parent}/{name}"
        superseded = inherited or node.get("superseded", False)
        containers[cid] = _prune({
            "id": cid, "type": "container", "label": name, "library": lib,
            "library_kind": kind, "n_decls": node["n_decls"],
            "n_direct": node.get("n_direct"),
            "superseded": True if superseded else None,
            "superseded_note": node.get("superseded_note"),
        })
        contains_edges.append(_edge(parent, cid, "contains", "theoremgraph",
                                    "hierarchy.json file-tree", pin_h, "high",
                                    {"n_decls": node["n_decls"]}))
        for child, sub in node.get("sub", {}).items():
            walk(lib, kind, child, sub, cid, superseded)

    # library roots that ARE Wikidata items get their identity on the node
    # (rendered as a Wikidata chip on the container panel) — extend as more
    # libraries gain items
    LIBRARY_QIDS = {"Mathlib": "Q140128421"}
    for lib, L in lib_meta.items():
        root = f"path:{lib}"
        containers[root] = _prune({"id": root, "type": "container", "label": lib,
                                   "library": lib, "library_kind": L["kind"],
                                   "qid": LIBRARY_QIDS.get(lib),
                                   "n_decls": L["n_decls"], "n_files": L["n_files"]})
        for name, node in L["modules"].items():
            walk(lib, L["kind"], name, node, root, False)

    # ---- decl nodes + their containment placement --------------------------
    def resolve(d: str) -> tuple[str, str | None]:
        module = _majority(mod_votes[d]) or csv_mod.get(d) or sf_mod.get(d)
        lib = _majority(lib_votes[d])
        if not lib:
            root = module.split(".", 1)[0] if module else None
            lib = root if root in lib_meta else "Mathlib"
        return lib, module

    # ---- Lean source snippets from the live checkout ------------------------
    # The snapshot CSV ships no statement bodies, so the decl panel's code
    # comes from the live mathlib4 checkout (Apache-2.0, attribution in _meta;
    # read-only; fail-soft on drift — a renamed file just means no snippet).
    mathlib_src = Path(os.environ.get(
        "BRAIN_MATHLIB_CHECKOUT", "/Users/jack/Desktop/LEAN/mathlib4/Mathlib")).parent
    kw = r"(?:theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque|axiom)"
    by_file: dict[str, list[str]] = defaultdict(list)
    for d in decl_set:
        lib, module = resolve(d)
        if lib == "Mathlib" and module:
            by_file[module].append(d)
    n_snippets = 0
    if mathlib_src.exists():
        for module, decls in by_file.items():
            fp = mathlib_src / (module.replace(".", "/") + ".lean")
            try:
                lines = fp.read_text().splitlines()
            except OSError:
                continue
            for d in decls:
                seg = re.escape(d.split(".")[-1])
                pat = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+"
                                 rf"|noncomputable\s+|nonrec\s+|scoped\s+)*{kw}\s+"
                                 rf"(?:[A-Za-z0-9_'.«»]+\.)?{seg}($|[^A-Za-z0-9_'])")
                for i, line in enumerate(lines):
                    if not pat.match(line):
                        continue
                    snip: list[str] = []
                    for l in lines[i:i + 12]:
                        s = l.rstrip()
                        if snip and not s:
                            break            # blank line = statement header over
                        snip.append(l)
                        if (s.endswith(":=") or s.endswith(":= by") or s.endswith(" by")
                                or s.endswith("where") or s.endswith(":= fun")):
                            break
                    code = "\n".join(snip)[:700]
                    decl_code.setdefault(d, {})["code"] = code
                    n_snippets += 1
                    break
    else:
        print(f"WARNING: mathlib checkout missing at {mathlib_src} — decl code "
              f"snippets skipped (BRAIN_MATHLIB_CHECKOUT to override)", file=sys.stderr)
    print(f"  decl code snippets from the checkout: {n_snippets}/{len(decl_set)}",
          file=sys.stderr)

    decl_id: dict[str, str] = {}
    decl_nodes: list[dict] = []
    n_unplaced = 0
    for d in sorted(decl_set):
        lib, module = resolve(d)
        did = f"decl:{lib}:{d}"
        decl_id[d] = did
        decl_nodes.append(_prune({
            "id": did, "type": "decl", "label": d, "library": lib,
            "module": module, "slogan": slogans.get(d), "pin": snapshot_pin,
            **decl_code.get(d, {}),
        }))
        # placement: deepest hierarchy container prefixing the decl's module
        # (the tree is depth-capped, so this is the file container when the
        # file node exists and the nearest enclosing dir otherwise)
        parts = module.split(".") if module else [lib]
        cur = f"path:{parts[0]}"
        if cur not in containers:
            n_unplaced += 1
            continue
        for comp in parts[1:]:
            if f"{cur}/{comp}" not in containers:
                break
            cur = f"{cur}/{comp}"
        contains_edges.append(_edge(cur, did, "contains", "theoremgraph",
                                    "module-prefix placement", pin_h, "high",
                                    _prune({"module": module})))

    # ---- mathlib source cross-reference tags --------------------------------
    n_source_tagged = 0
    emitted_formalizes: set[tuple[str, str]] = set()   # (qid, bare decl name)

    # ---- ontology edges -----------------------------------------------------
    edges: list[dict] = list(contains_edges)
    pin_g = _pin("concept_graph_v2.json")

    for n in graph["nodes"]:
        for f in n.get("formalizations") or []:
            gold = (n["qid"], f["decl"]) in source_tagged
            n_source_tagged += gold
            emitted_formalizes.add((n["qid"], f["decl"]))
            edges.append(_edge(n["qid"], decl_id[f["decl"]], "formalizes",
                               "mathlib",
                               "@[wikidata] attribute (mathlib4 source)" if gold
                               else "agent+oracle", pin_g,
                               f.get("confidence") or "medium",
                               _prune({"match_kind": f.get("match_kind"),
                                       "module": f.get("module"),
                                       "source_tagged": True if gold else None,
                                       "grounding_note": grounding_note.get(
                                           (n["qid"], f["decl"])),
                                       "verified_by": "build_graph_v2 oracle+checkout"})))

    pin_r = _pin("decl_qid_roles_v2.json")
    for q, d in mention_pairs:
        edges.append(_edge(q, decl_id[d], "mentions", "annotations",
                           "annotation-citation (decl_qid_roles_v2)", pin_r,
                           "high", ann_ev.get((q, d)) or {"role": "citation"}))

    for e in graph["edges"]:
        if e.get("source") != "mathlib":
            continue
        w = e.get("weight", 0)
        conf = "high" if w >= 5 else "medium" if w >= 2 else "low"
        edges.append(_edge(e["from"], e["to"], "depends", "mathlib_deps",
                           "lift_formal_edges (formal_dependency.csv)", pin_g, conf,
                           {"weight": w, "w_types": e.get("w_types"),
                            "witnesses": e.get("decls") or []}))

    pin_w = _pin("wikidata_edges.jsonl")
    rel_props: dict[tuple[str, str], list] = defaultdict(list)
    with INPUTS["wikidata_edges.jsonl"].open() as fh:
        for line in fh:
            r = json.loads(line)
            if r["s"] in qids and r["o"] in qids:
                rel_props[(r["s"], r["o"])].append({"p": r["p"], "label": r["p_label"]})
    for (s, o), props in sorted(rel_props.items()):
        props = sorted(props, key=lambda p: int(p["p"][1:]))
        edges.append(_edge(s, o, "relates", "wikidata_props", "wikidata-claims",
                           pin_w, "high", {"properties": props}))

    # One edge per (concept, source, page): the dst is the external PAGE id, so
    # two concepts sharing a MathWorld/nLab/LMFDB page become graph-discoverable
    # (the dst is an external identifier, not a node — see the P5d check below).
    pin_x = _pin("wikidata_crossrefs.json")
    n_xref_skipped_keys = 0
    seen_xref_dst: set[tuple[str, str]] = set()
    for n in graph["nodes"]:
        for key, values in sorted((n.get("xrefs") or {}).items()):
            if key not in XREF_KEYS:
                n_xref_skipped_keys += 1
                continue
            for v in sorted(values):
                # DLMF P11497 values are often equation-granular ('1.2.E34',
                # '25.12#ii') but the ingest mints SECTION pages ('1.2') — key
                # the dst at section level so the edge lands on a real node;
                # the raw value stays in evidence. Dedup: several equation
                # values can normalize onto one section.
                dst_id = v
                if key == "dlmf":
                    m = re.match(r"^(\d+\.\d+)(?:[.#]|$)", v)
                    if m:
                        dst_id = m.group(1)
                dst = f"xref:{key}:{dst_id}"
                if (n["qid"], dst) in seen_xref_dst:
                    continue
                seen_xref_dst.add((n["qid"], dst))
                edges.append(_edge(n["qid"], dst, "xref", key,
                                   "wikidata-property", pin_x, "high",
                                   {"property": XREF_KEYS[key], "value": v}))

    # decl → Stacks/Kerodon tag xrefs, only for decls that are already brain
    # nodes (rows for untracked decls are counted, never minted into nodes)
    n_tag_xref = n_tag_skipped = 0
    seen_tag: set[tuple[str, str]] = set()
    for r in tag_rows:
        if r["db"] not in ("stacks", "kerodon"):
            continue
        if r["decl"] not in decl_id:
            n_tag_skipped += 1
            continue
        key = (decl_id[r["decl"]], f"xref:{r['db']}:{r['tag']}")
        if key in seen_tag:
            continue
        seen_tag.add(key)
        n_tag_xref += 1
        edges.append(_edge(key[0], key[1], "xref", r["db"],
                           f"@[{r['db']}] attribute (mathlib4 source)",
                           snapshot_pin, "high",
                           {"tag": r["tag"], "value": r["tag"], "file": r["file"]}))
    if tag_rows:
        print(f"  mathlib tag xrefs: {n_tag_xref} stacks/kerodon edges "
              f"({n_tag_skipped} rows skipped — decl has no brain node); "
              f"{n_source_tagged} formalizes edges source-tagged @[wikidata]",
              file=sys.stderr)

    # ---- cites + matches (TheoremGraph links + transitive join) ------------
    pin_l = _pin("theoremgraph_links.json")
    pin_m = _pin("theorem_matching.csv")

    def judge_conf(g: str, d: str) -> str:
        return "high" if g == "exact" and d == "exact" else "medium"

    cites: dict[tuple[str, str], dict] = {}
    for q in sorted(links):
        for l in links[q]:
            lid = _lit_id(l["arxiv_id"], l["ref"])
            lit_title.setdefault(lid, l["title"])
            key = (q, lid)
            if key in cites:
                vd = cites[key]["evidence"]["via_decls"]
                if l["decl"] not in vd and len(vd) < 8:
                    vd.append(l["decl"])
                continue
            cites[key] = _edge(q, lid, "cites", "theoremgraph",
                               "theoremgraph_links", pin_l,
                               judge_conf(l["gpt54"], l["deepseek"]),
                               {"via_decls": [l["decl"]], "gpt54": l["gpt54"],
                                "deepseek": l["deepseek"], "sim": l["sim"],
                                "primary": l["primary"],
                                "license_open": lic_open.get(l["arxiv_id"])})
    n_cites_links = len(cites)
    match_rows.sort(key=lambda r: (r["decl"], r["lit"],
                                   -(r["gpt54"] == "exact" and r["deepseek"] == "exact"),
                                   -r["sim"]))
    matches: dict[tuple[str, str], dict] = {}
    for r in match_rows:
        mkey = (decl_id[r["decl"]], r["lit"])
        if mkey not in matches:
            matches[mkey] = _edge(mkey[0], r["lit"], "matches", "theoremgraph",
                                  "theorem_matching dual-judge", pin_m,
                                  judge_conf(r["gpt54"], r["deepseek"]),
                                  {"gpt54": r["gpt54"], "deepseek": r["deepseek"],
                                   "sim": r["sim"],
                                   "license_open": lic_open.get(r["arxiv_id"])})
        for q in sorted(set(fdecl_qids[r["decl"]])):  # transitive join, concept side
            key = (q, r["lit"])
            if key in cites:
                vd = cites[key]["evidence"]["via_decls"]
                if r["decl"] not in vd and len(vd) < 8:
                    vd.append(r["decl"])
                continue
            cites[key] = _edge(q, r["lit"], "cites", "theoremgraph",
                               "theorem_matching transitive-join", pin_m,
                               judge_conf(r["gpt54"], r["deepseek"]),
                               {"via_decls": [r["decl"]], "gpt54": r["gpt54"],
                                "deepseek": r["deepseek"], "sim": r["sim"],
                                "license_open": lic_open.get(r["arxiv_id"])})
    # links-file matches: every affirmed link row is also a decl→lit match
    for q in sorted(links):
        for l in links[q]:
            mkey = (decl_id[l["decl"]], _lit_id(l["arxiv_id"], l["ref"]))
            if mkey not in matches:
                matches[mkey] = _edge(mkey[0], mkey[1], "matches", "theoremgraph",
                                      "theoremgraph_links", pin_l,
                                      judge_conf(l["gpt54"], l["deepseek"]),
                                      {"gpt54": l["gpt54"], "deepseek": l["deepseek"],
                                       "sim": l["sim"],
                                       "license_open": lic_open.get(l["arxiv_id"])})
    edges.extend(cites[k] for k in sorted(cites))
    edges.extend(matches[k] for k in sorted(matches))

    # ---- fail-soft layers ---------------------------------------------------
    # Container links and discovery rows may introduce BRAND-NEW concepts (QIDs
    # outside the graph — fold_proposals fetched their labels/P31 into
    # universe_extension.jsonl) and brand-new decls (oracle/checkout-verified by
    # the fold). Create their nodes here so these layers genuinely GROW the
    # brain rather than only linking it.
    new_concepts: dict[str, dict] = {}
    new_decls: dict[str, dict] = {}
    universe_rec: dict[str, dict] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        if INPUTS[name].exists():
            with INPUTS[name].open() as fh:
                for line in fh:
                    r = json.loads(line)
                    universe_rec.setdefault(r["qid"], r)

    def ensure_concept(qid: str) -> bool:
        if qid in qids or qid in new_concepts:
            return True
        u = universe_rec.get(qid)
        if not u:
            return False
        new_concepts[qid] = _prune({
            "id": qid, "type": "concept", "label": u.get("label"),
            "slug": u.get("enwiki_slug"),
            "description": u.get("description"),
            "article_annotations": ann_summary.get(qid),
            "altitude_evidence": {"p31": u.get("classes") or [],
                                  "module_span": [], "match_kinds": []},
            "display": {"status": "partial"},
        })
        return True

    for _q in sorted(ann_new_concepts):   # every annotated article reaches the brain
        ensure_concept(_q)

    p = OPTIONAL_INPUTS["container_links.jsonl"]
    if p.exists():
        pin_c = _pin("container_links.jsonl")
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            path = rec["path"].removeprefix("path:").replace(".", "/")
            cid = f"path:{path}"
            if not ensure_concept(rec["qid"]) or cid not in containers:
                print(f"WARNING: container_links row skipped (unknown qid/path): "
                      f"{rec.get('qid')} -> {rec.get('path')}", file=sys.stderr)
                continue
            edges.append(_edge(rec["qid"], cid, "formalizes", "mathlib",
                               "container_links", pin_c,
                               rec.get("confidence") or "medium",
                               {"match_kind": rec.get("match_kind", "field"),
                                "note": rec.get("evidence")}))
    else:
        print("NOTE: brain/data/container_links.jsonl missing — "
              "concept→container formalizes layer skipped", file=sys.stderr)

    p = OPTIONAL_INPUTS["discovery_proposals.jsonl"]
    if p.exists():
        pin_d = _pin("discovery_proposals.jsonl")
        known = set(decl_id.values()) | set(containers) | qids
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            # only verifier-passed rows fold; rejected/unverified rows stay put
            if rec.get("rejected_reason") or rec.get("verified") is not True:
                continue
            src, dst = rec.get("src"), rec.get("dst")
            if rec.get("kind") not in KIND_ORDER:
                print(f"WARNING: discovery proposal skipped (unknown kind): "
                      f"{src} -{rec.get('kind')}-> {dst}", file=sys.stderr)
                continue
            if src not in known and not ensure_concept(src):
                print(f"WARNING: discovery src QID {src} has no universe "
                      f"record — row skipped", file=sys.stderr)
                continue
            if dst.startswith("decl:"):
                # The fold hardcodes lib=Mathlib; if resolve() already placed
                # this decl under another library (TheoremGraph module vote),
                # remap onto the existing node instead of forking a duplicate.
                bare = dst.split(":", 2)[2]
                if dst not in known and bare in decl_id:
                    dst = decl_id[bare]
            if dst not in known and dst not in new_decls and dst.startswith("decl:"):
                lib, d = dst.split(":", 2)[1:]
                module = rec.get("module")
                new_decls[dst] = _prune({
                    "id": dst, "type": "decl", "label": d, "library": lib,
                    "module": module, "slogan": slogans.get(d),
                    "pin": snapshot_pin,
                    **decl_code.get(d, {}),
                })
                parts = module.split(".") if module else [lib]
                cur = f"path:{parts[0]}"
                if cur in containers:
                    for comp in parts[1:]:
                        if f"{cur}/{comp}" not in containers:
                            break
                        cur = f"{cur}/{comp}"
                    edges.append(_edge(cur, dst, "contains", "theoremgraph",
                                       "module-prefix placement", pin_h, "high",
                                       _prune({"module": module})))
            ev = rec.get("evidence") or {}
            mk = ev.get("match_kind")
            if src in new_concepts and mk:
                ae = new_concepts[src]["altitude_evidence"]
                if mk not in ae["match_kinds"]:
                    ae["match_kinds"].append(mk)
                span = "/".join((rec.get("module") or "").split(".")[:2])
                if span and span not in ae["module_span"]:
                    ae["module_span"].append(span)
            if rec["kind"] == "formalizes" and dst.startswith("decl:"):
                bare = dst.split(":", 2)[2]
                emitted_formalizes.add((src, bare))
                if (src, bare) in source_tagged:   # gold pair found by another path
                    ev = {**ev, "source_tagged": True}
            edges.append(_edge(src, dst, rec["kind"], "mathlib",
                               "discovery_proposals (verified)", pin_d,
                               rec.get("confidence") or "medium", ev))
    else:
        print("NOTE: brain/data/discovery_proposals.jsonl missing — "
              "discovery layer skipped", file=sys.stderr)

    # Gold @[wikidata] pairs no pipeline found independently get minted here —
    # a maintainer-reviewed source tag IS a formalizes edge, the strongest kind
    # we have. Their decls joined decl_set above, so the decl node exists; the
    # QID must at least be known to the universe (else counted, never guessed).
    n_gold_minted = n_gold_unknown_qid = 0
    for qid, d in sorted(source_tagged - emitted_formalizes):
        if d not in decl_id:
            continue   # whitespace-filtered or unresolvable name
        if not ensure_concept(qid):
            n_gold_unknown_qid += 1
            continue
        n_gold_minted += 1
        n_source_tagged += 1
        edges.append(_edge(qid, decl_id[d], "formalizes", "mathlib",
                           "@[wikidata] attribute (mathlib4 source)",
                           snapshot_pin, "high",
                           {"match_kind": "exact", "source_tagged": True}))
    if source_tagged:
        print(f"  gold @[wikidata] pairs minted as new formalizes edges: "
              f"{n_gold_minted} ({n_gold_unknown_qid} skipped — QID not in the "
              f"universe)", file=sys.stderr)

    # ---- external DB pages → ext nodes + links edges (SCHEMA v2) ------------
    # Runs after every xref-emitting layer so anchoring sees the full dst set.
    # No catalog/data/external/ files → exact no-op (zero nodes, zero edges).
    registry = load_crossref_registry()
    ext_data = load_external(external_dir(), registry)
    all_qids = qids | set(new_concepts)
    xref_dsts: set[str] = set()
    concept_anchor: dict[str, set] = defaultdict(set)
    xref_pairs: set[tuple[str, str]] = set()
    for e in edges:
        if e["kind"] == "xref":
            xref_dsts.add(e["dst"])
            xref_pairs.add((e["src"], e["dst"]))
            if e["src"].startswith("Q"):
                concept_anchor[e["dst"]].add(e["src"])
    ext_nodes, ext_edges, ext_stats = external_layer(
        ext_data, concept_qids=all_qids, xref_dsts=xref_dsts,
        concept_anchor=concept_anchor, xref_pairs=xref_pairs,
        registry=registry, cap=ext_node_cap())
    edges.extend(ext_edges)
    if ext_data:
        print(f"  external layer: {sum(ext_stats['minted'].values())} ext nodes "
              f"({', '.join(f'{db}={n}' for db, n in sorted(ext_stats['minted'].items()))}); "
              f"{ext_stats['links_page']} page links, "
              f"{ext_stats['links_projected']} projected, "
              f"{ext_stats['xref_from_page_qid']} page-qid xrefs"
              + (f"; {ext_stats['anchors_outside_graph']} agent anchors point "
                 f"at QIDs outside the concept graph (inert until the concept "
                 f"is minted)" if ext_stats.get('anchors_outside_graph') else ""),
              file=sys.stderr)
    else:
        print("NOTE: catalog/data/external/ empty — ext nodes / links edges "
              "skipped (brain/ingest adapters not run)", file=sys.stderr)

    # ---- literature papers: lit:<arxiv_id> + containment + bibliography -----
    cit_path = external_dir() / "arxiv_citations.jsonl"
    paper_nodes, lit_edges, lit_stats = literature_layer(
        lit_title, lic_open, cit_path, pin_l)
    edges.extend(lit_edges)
    print(f"  literature papers: {lit_stats['papers']} "
          f"({lit_stats['papers_new']} minted — the rest double as empty-ref "
          f"statements), {lit_stats['contains']} contains, "
          f"{lit_stats['citations']} bibliography links "
          f"({lit_stats['citation_rows_dropped']} rows dropped)", file=sys.stderr)
    if not cit_path.exists():
        print("NOTE: catalog/data/external/arxiv_citations.jsonl missing — "
              "paper→paper bibliography links skipped "
              "(brain/ingest/openalex_citations.py)", file=sys.stderr)

    # ---- concept nodes -------------------------------------------------------
    p31: dict[str, list[str]] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        with INPUTS[name].open() as fh:
            for line in fh:
                r = json.loads(line)
                merged = p31.setdefault(r["qid"], [])
                merged.extend(c for c in r.get("classes") or [] if c not in merged)

    concept_nodes = []
    for n in graph["nodes"]:
        span = sorted({"/".join((f.get("module") or "").split(".")[:2])
                       for f in n.get("formalizations") or [] if f.get("module")})
        concept_nodes.append(_prune({
            "id": n["qid"], "type": "concept", "label": n.get("label"),
            "slug": n.get("slug"),
            "article_annotations": ann_summary.get(n["qid"]),
            # Google KG is a hub id (never an xref edge — SCHEMA) but a useful
            # "Also in" chip; carried on the node payload instead
            "kgmid": ((n.get("xrefs") or {}).get("kgmid") or [None])[0],
            "altitude_evidence": {
                "p31": p31.get(n["qid"], []),
                "module_span": span,
                "match_kinds": sorted({f.get("match_kind")
                                       for f in n.get("formalizations") or []
                                       if f.get("match_kind")}),
                "msc": sorted((n.get("xrefs") or {}).get("msc", [])),
            },
            "display": _prune({"primary_decl": n.get("primary_decl"),
                               "status": n.get("status"),
                               "importance": n.get("importance")}),
        }))

    concept_nodes.extend(new_concepts[q] for q in sorted(new_concepts))
    decl_nodes.extend(new_decls[d] for d in sorted(new_decls))

    lit_nodes = [_prune({
        "id": lid, "type": "literature",
        "label": lit_title.get(lid) or lid,
        "arxiv_id": lid[4:].split("#", 1)[0],
        "ref": lid.split("#", 1)[1] if "#" in lid else "",
        "license_open": lic_open.get(lid[4:].split("#", 1)[0]),
        "session_keys": lit_sids.get(lid),
    }) for lid in sorted(lit_title)]
    # paper-level nodes interleave in id order (ids are disjoint from the
    # statement set by construction — literature_layer never re-mints an
    # empty-ref statement's id)
    lit_nodes = sorted(lit_nodes + paper_nodes, key=lambda n: n["id"])

    nodes = (sorted(concept_nodes, key=lambda n: int(n["id"][1:]))
             + [containers[k] for k in sorted(containers)]
             + decl_nodes + lit_nodes
             + sorted(ext_nodes, key=lambda n: n["id"]))

    # ---- v2 unit cards + facet bitmasks (need the complete node/edge sets) ---
    descriptions: dict[str, str] = {}
    p = OPTIONAL_INPUTS["wikidata_descriptions.json"]
    if p.exists():
        raw = json.loads(p.read_text())
        # ingest writes {_meta, descriptions:{qid: text}}; tolerate the old
        # flat {qid: text} shape too
        raw = raw.get("descriptions", raw) if isinstance(raw, dict) else {}
        descriptions = {k: v for k, v in raw.items()
                        if k.startswith("Q") and isinstance(v, str)}
    else:
        print("NOTE: catalog/data/wikidata_descriptions.json missing — "
              "unit.description falls back to universe descriptions",
              file=sys.stderr)
    assemble_units(nodes, edges, descriptions, registry)
    apply_facets(nodes, edges, tag_rows)
    aggregate_facets(nodes, edges)

    edges.sort(key=lambda e: (KIND_ORDER.index(e["kind"]), e["src"], e["dst"]))

    # every non-xref endpoint must be a real node (xref dst is the external DB)
    ids = {n["id"] for n in nodes}
    dangling = [e for e in edges
                if e["src"] not in ids or (e["kind"] != "xref" and e["dst"] not in ids)]
    if dangling:
        raise SystemExit(f"BUG: {len(dangling)} edges with dangling endpoints, "
                         f"first: {dangling[0]}")

    present = {**INPUTS, **{k: v for k, v in OPTIONAL_INPUTS.items() if v.exists()}}
    for rec in ext_data.values():
        for ep in rec["paths"]:
            present[f"external/{ep.name}"] = ep
    if cit_path.exists():
        present[f"external/{cit_path.name}"] = cit_path
    newest = max(v.stat().st_mtime for v in present.values())
    meta = {
        "schema": "brain/SCHEMA.md",
        # newest input mtime, NOT build time — rebuilds of the same inputs are stable
        "generated_at": datetime.fromtimestamp(newest, tz=timezone.utc)
                        .isoformat(timespec="seconds"),
        "inputs": {k: {"mtime": datetime.fromtimestamp(v.stat().st_mtime, tz=timezone.utc)
                       .isoformat(timespec="seconds"), "bytes": v.stat().st_size}
                   for k, v in sorted(present.items())},
        "licenses": {
            "brain": "CC0-1.0 (WikiLean's own node/edge data)",
            "theoremgraph": links_meta["attribution"],
            "slogans": "decl slogans are currently ABSENT by policy: "
                       "theorem_matching.csv's formal_slogan is license-contested "
                       "upstream (CC-BY-SA card vs CC-BY-NC-SA paper, BRAIN.md:452) "
                       "so it stays link-facts-only, and slogan.csv (CC-BY-4.0) "
                       "turned out to cover informal statements exclusively "
                       "(0/2.57M rows reference a formal id). Decl gloss = "
                       "docstring + code, both cleanly licensed.",
            "code": "decl `code` snippets are statement headers read from the live "
                    "mathlib4 checkout — Apache-2.0 (mathlib4 contributors), render "
                    "with source credit; `docstring`/`decl_kind` from TheoremGraph "
                    "statement_formal.csv (CC-BY-4.0)",
            "arxiv": "arXiv statement text is never redistributed — ids/titles/labels only",
            "wikidata": "CC0-1.0",
            "mathlib_tags": "@[stacks]/@[kerodon]/@[wikidata] cross-reference tags "
                            "harvested from the mathlib4 source (Apache-2.0, mathlib4 "
                            "contributors) — human-reviewed gold links",
            "external": "ext node ids/titles/urls/links are CC0 link facts; "
                        "stored snippets carry a per-node snippet_license and "
                        "exist ONLY for license-permitting sources "
                        "(source_registry ingest.snippets) — no-content sources "
                        "(mathworld/dlmf/eom/kerodon) ship ids+titles+links only",
        },
        "counts": {
            "nodes": dict(sorted(Counter(n["type"] for n in nodes).items())),
            "edges": {k: c for k, c in
                      sorted(Counter(e["kind"] for e in edges).items(),
                             key=lambda kv: KIND_ORDER.index(kv[0]))},
        },
        "notes": {
            "decls_without_module": len([d for d in decl_set
                                         if not resolve(d)[1]]),
            "decls_unplaced": n_unplaced,
            "cites_from_links": n_cites_links,
            "cites_from_transitive_join": len(cites) - n_cites_links,
            "xref_values_skipped_nonschema_keys": n_xref_skipped_keys,
            "mathlib_tag_xref_edges": n_tag_xref,
            "mathlib_tag_rows_skipped_no_decl_node": n_tag_skipped,
            "formalizes_source_tagged": n_source_tagged,
            "ext_nodes_minted": dict(sorted(ext_stats["minted"].items())),
            "ext_nodes_capped": dict(sorted(ext_stats["capped"].items())),
            "links_page_edges": ext_stats["links_page"],
            "links_projected_edges": ext_stats["links_projected"],
            "xref_edges_from_page_qids": ext_stats["xref_from_page_qid"],
            "lit_papers": lit_stats["papers"],
            "lit_paper_nodes_minted": lit_stats["papers_new"],
            "lit_contains_edges": lit_stats["contains"],
            "links_bibliography_edges": lit_stats["citations"],
            "lit_citation_rows_dropped": lit_stats["citation_rows_dropped"],
        },
    }
    return nodes, edges, meta


def write_jsonl(out: Path, meta: dict, rows: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(out)


def write_edges(edges: list[dict], meta: dict,
                out: Path = EDGES_OUT, out_links: Path = EDGES_LINKS_OUT) -> dict:
    """Write the edge set split across two files.

    `out` gets every kind EXCEPT `links`, with the FULL build meta unchanged —
    byte-compatible with the historical single file minus its links rows
    (links sort last in KIND_ORDER, so the non-links rows are its exact
    prefix; _meta.counts still describes the whole edge set). `out_links`
    gets only kind=='links' rows under a small _meta of its own. Both writes
    are atomic; the links file is always (re)written — a zero-links build
    leaves an empty-row file rather than a stale one.
    """
    main_rows = [e for e in edges if e["kind"] != "links"]
    links_rows = [e for e in edges if e["kind"] == "links"]
    write_jsonl(out, meta, main_rows)
    notes = meta.get("notes") or {}
    links_meta = {
        "schema": meta.get("schema", "brain/SCHEMA.md"),
        "generated_at": meta.get("generated_at"),
        "split_from": out.name,
        "note": "kind=='links' rows split out of edges.jsonl (GitHub 100 MB "
                "per-file limit). Gitignored — rebuild deterministically with "
                "`python3 brain/build_edges.py` from the committed "
                "catalog/data/external/ inputs. Readers treat a missing file "
                "as empty; row schema is identical to edges.jsonl.",
        "counts": {"edges": {"links": len(links_rows)},
                   "page_level": notes.get("links_page_edges"),
                   "projected": notes.get("links_projected_edges"),
                   # paper→paper bibliography links (openalex); key absent
                   # on pre-literature-layer metas
                   **({"bibliography": notes["links_bibliography_edges"]}
                      if "links_bibliography_edges" in notes else {})},
    }
    write_jsonl(out_links, links_meta, links_rows)
    return {"main": len(main_rows), "links": len(links_rows)}
