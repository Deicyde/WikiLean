#!/usr/bin/env python3
"""LMFDB knowl ingest (FULL) — Postgres devmirror, table kwl_knowls.

Uses the public read-only mirror (devmirror.lmfdb.xyz, lmfdb/lmfdb) instead of
crawling (lmfdb.org robots crawl-delay is 30s; the mirror is the sanctioned
bulk path). Latest revision per knowl id; keeps reviewed (status=1) and beta
(status=0) NORMAL knowls only — type=2 column knowls and type=1/-1 top/bottom
template knowls are schema plumbing, type=-2 comments are talk. Snippets are
CC-BY-SA 4.0. pg8000 is the one sanctioned non-stdlib dependency (LMFDB only).

Run: python3 brain/ingest/lmfdb.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

import pg8000.native

HOST, PORT, DBNAME, USER, PASSWORD = "devmirror.lmfdb.xyz", 5432, "lmfdb", "lmfdb", "lmfdb"
URL = "https://www.lmfdb.org/knowledge/show/{}"

BRACE = re.compile(r"\{\{(.*?)\}\}", re.S)
# positional ('x') vs kwarg (k='x') args inside a {{ KNOWL(...) }} template
ARG = re.compile(r"""(\w+\s*=\s*)?('([^']*)'|"([^"]*)")""")
WIKIDATA_KW = re.compile(r"""wikidata\s*=\s*["'](Q\d+)["']""")


def template_args(inner: str) -> tuple[list[str], dict[str, str]]:
    pos, kw = [], {}
    body = inner[inner.find("(") + 1:]
    for m in ARG.finditer(body):
        val = m.group(3) if m.group(3) is not None else m.group(4)
        if m.group(1):
            kw[m.group(1).split("=")[0].strip()] = val
        else:
            pos.append(val)
    return pos, kw


def strip_templates(content: str) -> str:
    """{{KNOWL('id','text')}} -> text; {{KNOWL_INC(..)}} -> ''; {{DEFINES(..)}} -> term."""
    def repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        if inner.startswith("KNOWL_INC"):
            return ""
        if inner.startswith("KNOWL"):
            pos, kw = template_args(inner)
            return kw.get("title") or (pos[1] if len(pos) > 1 else (pos[0] if pos else ""))
        if inner.startswith("DEFINES"):
            pos, _ = template_args(inner)
            return pos[0] if pos else ""
        return ""  # any other jinja bit (e.g. {% .. %} handled below)
    content = BRACE.sub(repl, content)
    return re.sub(r"\{%.*?%\}", "", content, flags=re.S)


def main() -> int:
    con = pg8000.native.Connection(
        user=USER, password=PASSWORD, host=HOST, port=PORT, database=DBNAME,
        timeout=60)
    try:
        cols = {r[0] for r in con.run(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='kwl_knowls'")}
        required = {"id", "title", "content", "links", "status", "timestamp"}
        missing = required - cols
        if missing:
            raise RuntimeError(f"kwl_knowls schema drifted; missing {missing}")
        type_filter = "AND t.type = 0" if "type" in cols else ""
        rows = con.run(
            "SELECT id, title, content, links, timestamp FROM ("
            "  SELECT DISTINCT ON (id) * FROM kwl_knowls ORDER BY id, timestamp DESC"
            f") t WHERE t.status IN (0, 1) {type_filter} ORDER BY id")
    finally:
        con.close()

    qids = common.qid_map("lmfdb_knowl")
    pages, links = [], set()
    latest = None
    for kid, title, content, link_arr, ts in rows:
        content = content or ""
        if latest is None or (ts and ts > latest):
            latest = ts
        row = {"db": "lmfdb_knowl", "id": kid,
               "title": (title or "").strip() or kid,
               "url": URL.format(kid), "kind_hint": "knowl"}
        snippet = strip_templates(content)
        if snippet.strip():
            row["snippet"] = snippet
        # LMFDB's own DEFINES(wikidata=...) curation — but never for doc.*
        # meta-knowls (editing guidelines etc. quote the macro as an EXAMPLE,
        # which would anchor documentation pages to math concepts)
        m = None if kid.startswith("doc.") else WIKIDATA_KW.search(content)
        qid = m.group(1) if m else qids.get(kid)
        if qid:
            row["qid"] = qid
        pages.append(row)
        for dst in link_arr or []:
            dst = (dst or "").strip()
            if dst and dst != kid:
                links.add((kid, dst))

    link_rows = [{"db": "lmfdb_knowl", "src": s, "dst": d, "context": "body"}
                 for s, d in sorted(links)]
    common.emit("lmfdb_knowl", pages, link_rows, extra_meta={
        "source_pin": f"devmirror.lmfdb.xyz kwl_knowls latest_ts={latest}",
        "n_with_qid": sum(1 for p in pages if "qid" in p),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
