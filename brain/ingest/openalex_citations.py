#!/usr/bin/env python3
"""arXiv paper→paper bibliography edges via OpenAlex `referenced_works`.

The Brain's literature layer (`lit:<arxiv_id>#<ref>` statement nodes from
TheoremGraph) had no links BETWEEN papers. This adapter collects the distinct
arXiv ids of catalog/data/theoremgraph_links.json (the statement links' own
arxiv_id field — build inputs only, never brain/data) and emits every OpenAlex
citation edge whose BOTH endpoints are in that set.

Empirical findings (2026-07-11) that shaped the pipeline — the naive route
(batch DOI filter + intersect referenced_works) yielded only 10 edges:
  - `filter=ids.arxiv:` / `filter=arxiv_id:` do NOT exist; the working batch
    route is `filter=doi:10.48550/arXiv.<id>|...` (resolves new-style
    2306.01234 AND old-style quant-ph/0511145 ids).
  - OpenAlex's FILTER index is missing DOIs its ENTITY endpoint resolves
    (e.g. 10.48550/arXiv.1808.04180) → phase A falls back to a direct
    `GET /works/https://doi.org/...` for filter misses.
  - Preprint-only works usually have EMPTY referenced_works (1,169/1,788 of
    ours), and other works' references point at the PUBLISHED twin, not the
    preprint → phase A2 crosswalks each arXiv id to its journal DOI via
    arXiv's own metadata API (exact, author-supplied; ~51% coverage of the
    published subset) and resolves that DOI to the published OpenAlex work;
    phase B identifies every referenced work by its arXiv DOI/location URL;
    phase C pulls referenced_works from all published twins of OUR papers
    (one bounded round), so both edge directions recover the
    published-venue citation graph.

Phases (all cached under catalog/.cache/external/openalex/, trusted
unconditionally on later runs — delete files there to force a re-fetch):
  A.  arXiv id → work: batch DOI filter, then direct-GET fallback  works/<id>.json
  A2. arXiv id → journal DOI (export.arxiv.org, 3 s etiquette)     arxiv_meta/<id>.json
      journal DOI → published OpenAlex work                        jwork/<id>.json
  B.  referenced W-id → arXiv id: batch fetch id,doi,locations     refs/<W>.json
  C.  published twins of our ids (A2 + B): referenced_works        twin_refs/<W>.json
      (their new refs get one more round of B identification)

Output — a bespoke citations artifact, NOT the <db>_pages/links contract:

  catalog/data/external/arxiv_citations.jsonl
    {"_meta": {...}}                             # first line
    {"src": "<arxiv_id>", "dst": "<arxiv_id>"}   # src's bibliography cites dst

build_common.literature_layer() turns these rows into paper→paper `links`
edges (evidence.context="bibliography"). OpenAlex data is CC0 — no licensing
constraints. Rate norms (100k requests/day, 10/s) are far above our few
hundred calls; a polite 0.2 s delay rides on every call anyway. curl_fetch
throughout (the system Python SSL trust store is broken on this machine).
Atomic fail-soft output: any fetch failure raises before the write, leaving
the previous file intact, and the volume sanity floor refuses a suspiciously
shrunken edge set (BRAIN_INGEST_FORCE=1 overrides).

Run: python3 brain/ingest/openalex_citations.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

API = "https://api.openalex.org/works"
OUT = common.EXTERNAL_DIR / "arxiv_citations.jsonl"
LINKS_JSON = common.REPO / "catalog" / "data" / "theoremgraph_links.json"
BATCH = 50      # values per OR-filter request (OpenAlex documented max)
DELAY = 0.2     # polite; norms are 10 req/s / 100k req/day
# the WikiLean contact from common.USER_AGENT, passed as OpenAlex's mailto param
MAILTO = common.USER_AGENT.split("(", 1)[1].split(";", 1)[0]

# arXiv id shapes: new-style 2306.01234, old-style quant-ph/0511145 or
# math.GT/0211159. TheoremGraph's arxiv_id column also carries GitHub repos
# (teorth/pfr, ImperialCollegeLondon/FLT, ...) — those match neither shape and
# are skipped (counted in _meta.n_skipped_non_arxiv).
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"
ARXIV_RE = re.compile(rf"^{ARXIV_ID}(?:v\d+)?$")
ABS_URL_RE = re.compile(rf"arxiv\.org/(?:abs|pdf)/({ARXIV_ID})(?:v\d+)?")

_calls = 0


def api_get(url: str) -> bytes:
    global _calls
    if _calls:
        time.sleep(DELAY)
    _calls += 1
    return common.curl_fetch(url)


def collect_arxiv_ids() -> tuple[list[str], int]:
    doc = json.loads(LINKS_JSON.read_text())
    ids = {l["arxiv_id"] for ls in doc["links"].values() for l in ls
           if l.get("arxiv_id")}
    good = sorted(i for i in ids if ARXIV_RE.match(i))
    return good, len(ids) - len(good)


def doi_of(aid: str) -> str:
    # version suffixes never appear in theoremgraph ids, but strip defensively:
    # the arXiv DOI is registered against the versionless id
    return "10.48550/arXiv." + re.sub(r"v\d+$", "", aid)


def batch_url(filt: str, select: str) -> str:
    return API + "?" + urllib.parse.urlencode({
        "filter": filt, "select": select,
        # 50 unique filter values can match >50 works only via data-quality
        # duplicates; 100 headroom + the count check below keeps us honest
        "per-page": "100", "mailto": MAILTO})


def batch_results(filt: str, select: str) -> list[dict]:
    data = json.loads(api_get(batch_url(filt, select)))
    if not isinstance(data, dict) or "error" in data or "results" not in data:
        head = (json.dumps(data, ensure_ascii=False)[:300]
                if isinstance(data, (dict, list)) else repr(data)[:300])
        raise RuntimeError(f"openalex batch returned an error/malformed "
                           f"response (aborting before emit): {head}")
    if data["meta"]["count"] > len(data["results"]):
        raise RuntimeError(f"openalex batch overflow: count="
                           f"{data['meta']['count']} > page of "
                           f"{len(data['results'])} — shrink BATCH")
    return data["results"]


def cache_read(p: Path) -> dict | None:
    return json.loads(p.read_text()) if p.exists() else None


def cache_write(p: Path, rec: dict) -> None:
    common.atomic_write_bytes(p, json.dumps(rec, ensure_ascii=False).encode())


def arxiv_of_work(w: dict) -> str | None:
    """arXiv id of an OpenAlex work: its 10.48550 DOI (preprint records) or an
    arxiv.org/abs|pdf location URL (published records that subsume the
    preprint). OpenAlex lowercases DOIs; location URLs keep original case."""
    doi = (w.get("doi") or "").lower()
    if "/10.48550/arxiv." in doi:
        return re.sub(r"v\d+$", "", doi.split("/10.48550/arxiv.", 1)[1])
    for loc in w.get("locations") or []:
        for u in (loc.get("landing_page_url"), loc.get("pdf_url")):
            m = ABS_URL_RE.search(u or "")
            if m:
                return m.group(1)
    return None


# ---- phase A: our arXiv ids → works (DOI batch filter + direct-GET fallback) --

def phase_a(aids: list[str]) -> dict[str, dict]:
    """Returns {arxiv_id: {"openalex_id": W|None, "referenced_works": [...]}}."""
    cpath = {a: common.cache_path("openalex", "works", a.replace("/", "__") + ".json")
             for a in aids}
    todo = [a for a in aids if cache_read(cpath[a]) is None]
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        filt = "doi:" + "|".join(doi_of(a) for a in chunk)
        by_doi = {f"https://doi.org/{doi_of(a)}".lower(): a for a in chunk}
        for w in batch_results(filt, "id,doi,referenced_works"):
            aid = by_doi.get((w.get("doi") or "").lower())
            if aid:
                cache_write(cpath[aid], {
                    "arxiv_id": aid, "openalex_id": w["id"],
                    "referenced_works": w.get("referenced_works") or []})
        for a in chunk:   # filter misses: leave for the direct-GET fallback
            if cache_read(cpath[a]) is None:
                cache_write(cpath[a], {"arxiv_id": a, "openalex_id": None,
                                       "referenced_works": []})
    # direct-GET fallback: the filter index is missing DOIs the entity
    # endpoint resolves (verified: 10.48550/arXiv.1808.04180). Only records
    # that never got the fallback (no "direct_checked") are probed.
    n_direct = 0
    for a in aids:
        rec = cache_read(cpath[a])
        if rec.get("openalex_id") or rec.get("direct_checked"):
            continue
        n_direct += 1
        try:
            w = json.loads(api_get(
                f"{API}/https://doi.org/{urllib.parse.quote(doi_of(a), safe='/:.')}"
                f"?select=id,doi,referenced_works&mailto={urllib.parse.quote(MAILTO)}"))
            rec = {"arxiv_id": a, "openalex_id": w["id"],
                   "referenced_works": w.get("referenced_works") or [],
                   "direct_checked": True}
        except RuntimeError as e:
            if "(22)" not in str(e):   # 22 = curl HTTP >=400 (404 not-found)
                raise                  # network failure must NOT cache as negative
            rec = {"arxiv_id": a, "openalex_id": None,
                   "referenced_works": [], "direct_checked": True}
        cache_write(cpath[a], rec)
        if n_direct % 100 == 0:
            print(f"[openalex] direct-GET fallback: {n_direct} probed",
                  file=sys.stderr)
    return {a: cache_read(cpath[a]) for a in aids}


# ---- phase A2: arXiv id → journal DOI → published OpenAlex work ---------------

def phase_a2(aids: list[str]) -> dict[str, str]:
    """arXiv id → journal DOI from arXiv's OWN metadata (the <arxiv:doi>
    element — exact, author-supplied). export.arxiv.org atom feed, 100 ids
    per call, 3 s etiquette delay (arXiv API norms, stricter than ours)."""
    cpath = {a: common.cache_path("openalex", "arxiv_meta",
                                  a.replace("/", "__") + ".json") for a in aids}
    todo = [a for a in aids if cache_read(cpath[a]) is None]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        if i:
            time.sleep(3.0)
        xml = common.curl_fetch(
            "https://export.arxiv.org/api/query?id_list=" + ",".join(chunk)
            + f"&max_results={len(chunk)}").decode("utf-8", "replace")
        entries = xml.split("<entry>")[1:]
        if not entries:
            raise RuntimeError("arxiv api returned no entries for a non-empty "
                               "id_list (aborting before emit)")
        found: dict[str, str | None] = {}
        for ent in entries:
            m = re.search(r"<id>https?://arxiv\.org/abs/(.+?)(?:v\d+)?</id>", ent)
            if not m:
                continue
            d = re.search(r"<arxiv:doi[^>]*>([^<]+)</arxiv:doi>", ent)
            found[m.group(1)] = d.group(1).strip() if d else None
        for a in chunk:
            cache_write(cpath[a], {"arxiv_id": a, "journal_doi": found.get(a)})
    out: dict[str, str] = {}
    for a in aids:
        rec = cache_read(cpath[a]) or {}
        doi = (rec.get("journal_doi") or "").strip().rstrip(".,;")
        # author-supplied DOIs carry typos; a ',' or '|' inside a filter value
        # is an OpenAlex separator (a trailing comma 400'd a whole batch) —
        # keep only clean-shaped DOIs
        if doi and re.fullmatch(r"10\.\d{4,9}/[^\s,|]+", doi):
            out[a] = doi
    return out


def phase_jwork(doi_by_aid: dict[str, str]) -> dict[str, str | None]:
    """journal DOI → published OpenAlex W-id (batch filter), keyed by arXiv id.
    No entity-GET fallback here: a filter-index miss just loses one twin."""
    cpath = {a: common.cache_path("openalex", "jwork",
                                  a.replace("/", "__") + ".json")
             for a in doi_by_aid}
    todo = sorted(a for a in doi_by_aid if cache_read(cpath[a]) is None)
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        by_doi = {f"https://doi.org/{doi_by_aid[a]}".lower(): a for a in chunk}
        try:
            results = batch_results(
                "doi:" + "|".join(doi_by_aid[a] for a in chunk), "id,doi")
        except RuntimeError:
            # one malformed author-supplied DOI can 400 the whole OR batch —
            # degrade to per-DOI requests so a bad apple only loses itself
            results = []
            for a in chunk:
                try:
                    results.extend(batch_results("doi:" + doi_by_aid[a], "id,doi"))
                except RuntimeError:
                    pass
        for w in results:
            aid = by_doi.get((w.get("doi") or "").lower())
            if aid:
                cache_write(cpath[aid], {"arxiv_id": aid,
                                         "doi": doi_by_aid[aid],
                                         "openalex_id": w["id"]})
        for a in chunk:
            if cache_read(cpath[a]) is None:
                cache_write(cpath[a], {"arxiv_id": a, "doi": doi_by_aid[a],
                                       "openalex_id": None})
    return {a: (cache_read(cpath[a]) or {}).get("openalex_id")
            for a in doi_by_aid}


# ---- phase B: referenced W-ids → arXiv ids ------------------------------------

def phase_b(wids: set[str]) -> dict[str, str | None]:
    """Returns {W-id: arxiv_id|None} for every id in wids."""
    cpath = {w: common.cache_path("openalex", "refs", w.rsplit("/", 1)[1] + ".json")
             for w in wids}
    todo = sorted(w for w in wids if cache_read(cpath[w]) is None)
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        filt = "openalex_id:" + "|".join(w.rsplit("/", 1)[1] for w in chunk)
        got: set[str] = set()
        for w in batch_results(filt, "id,doi,locations"):
            got.add(w["id"])
            if w["id"] in cpath:
                cache_write(cpath[w["id"]], {"id": w["id"],
                                             "arxiv_id": arxiv_of_work(w)})
        for w in chunk:   # merged-away/dead ids the filter can't see
            if w not in got:
                cache_write(cpath[w], {"id": w, "arxiv_id": None})
        if (i // BATCH) % 25 == 24:
            print(f"[openalex] ref identification: {i + BATCH}/{len(todo)}",
                  file=sys.stderr)
    return {w: (cache_read(cpath[w]) or {}).get("arxiv_id") for w in wids}


# ---- phase C: referenced_works of published twins of OUR papers ---------------

def phase_c(twin_wids: set[str]) -> dict[str, list[str]]:
    """Returns {W-id: referenced_works} for the given twin work ids."""
    cpath = {w: common.cache_path("openalex", "twin_refs",
                                  w.rsplit("/", 1)[1] + ".json")
             for w in twin_wids}
    todo = sorted(w for w in twin_wids if cache_read(cpath[w]) is None)
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        filt = "openalex_id:" + "|".join(w.rsplit("/", 1)[1] for w in chunk)
        got: set[str] = set()
        for w in batch_results(filt, "id,referenced_works"):
            got.add(w["id"])
            if w["id"] in cpath:
                cache_write(cpath[w["id"]], {
                    "id": w["id"],
                    "referenced_works": w.get("referenced_works") or []})
        for w in chunk:
            if w not in got:
                cache_write(cpath[w], {"id": w, "referenced_works": []})
    return {w: (cache_read(cpath[w]) or {}).get("referenced_works") or []
            for w in twin_wids}


def main() -> int:
    aids, n_skipped = collect_arxiv_ids()
    print(f"[openalex] {len(aids)} arXiv ids from {LINKS_JSON.name} "
          f"({n_skipped} non-arXiv ids skipped)", file=sys.stderr)

    ours = phase_a(aids)
    w2aid: dict[str, str] = {}          # any W-id known to BE one of our papers
    refs_by_aid: dict[str, set[str]] = {a: set() for a in aids}
    for a, rec in ours.items():
        if rec.get("openalex_id"):
            w2aid[rec["openalex_id"]] = a
            refs_by_aid[a].update(rec["referenced_works"])
    n_resolved = len(w2aid)
    print(f"[openalex] phase A: {n_resolved}/{len(aids)} ids resolved "
          f"({sum(len(r) for r in refs_by_aid.values())} refs)", file=sys.stderr)

    # A2: exact arXiv→journal-DOI crosswalk, then the published twin's W-id
    jdois = phase_a2(aids)
    jwork = phase_jwork(jdois)
    jtwins = {w: a for a, w in sorted(jwork.items()) if w and w not in w2aid}
    w2aid.update(jtwins)
    print(f"[openalex] phase A2: {len(jdois)} journal DOIs (arXiv metadata); "
          f"{len(jtwins)} published twins resolved", file=sys.stderr)

    all_refs = set().union(*refs_by_aid.values()) if refs_by_aid else set()
    ident = phase_b(all_refs - set(w2aid))
    btwins = {w for w, ax in ident.items() if ax in refs_by_aid and w not in w2aid}
    for w in btwins:                    # published twins of OUR papers
        w2aid[w] = ident[w]
    print(f"[openalex] phase B: {sum(1 for v in ident.values() if v)}/"
          f"{len(ident)} referenced works carry an arXiv id; "
          f"{len(btwins)} more are published twins of our papers", file=sys.stderr)

    twins = set(jtwins) | btwins
    twin_refs = phase_c(twins)
    new_refs: set[str] = set()
    for w, refs in twin_refs.items():
        refs_by_aid[w2aid[w]].update(refs)
        new_refs.update(refs)
    # one more identification round for refs the twins introduced (bounded:
    # twins-of-twins are NOT expanded further)
    ident2 = phase_b(new_refs - set(w2aid) - set(ident))
    for w, ax in ident2.items():
        if ax in refs_by_aid:
            w2aid[w] = ax
    print(f"[openalex] phase C: {len(twins)} twins added "
          f"{sum(len(r) for r in twin_refs.values())} refs "
          f"({len(ident2)} newly identified)", file=sys.stderr)

    pairs: set[tuple[str, str]] = set()
    for src, refs in refs_by_aid.items():
        for w in refs:
            dst = w2aid.get(w)
            if dst and dst != src:
                pairs.add((src, dst))
    rows = [{"src": s, "dst": d} for s, d in sorted(pairs)]

    meta = {
        "db": "openalex",
        "fetched_at": common.now_iso(),
        "source_pin": "api.openalex.org /works: doi:10.48550/arXiv.<id> batch "
                      "filter + entity-GET fallback; referenced works "
                      "identified by arXiv DOI/location; published-twin "
                      "referenced_works folded in (one round)",
        "license": "CC0 (OpenAlex)",
        "meaning": "src's bibliography cites dst; both endpoints restricted to "
                   "the theoremgraph_links.json arXiv id set",
        "n_arxiv_ids": len(aids),
        "n_skipped_non_arxiv": n_skipped,
        "n_resolved": n_resolved,
        "n_twins": len(twins),
        "n_api_calls": _calls,
        "n_links": len(rows),
    }
    common._volume_guard(OUT, "link", len(rows))
    common.write_jsonl(OUT, meta, rows)
    print(f"[openalex] {n_resolved}/{len(aids)} papers resolved "
          f"(+{len(twins)} published twins); {len(rows)} both-endpoints "
          f"citation edges -> {OUT} ({_calls} API calls)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
