"""The prototype name index for scope B: every name of both doc-gen4 indexes, one table.

    decl_names(seq, name, library, module, doc_link)   UNIQUE(name, library)
    decl_names_fts                                     FTS5 trigram over name, rowid = seq

A harness prototype only: build_serving_db.py does not build it and the importer does not copy
it. The rows run library by library (in the order given), names sorted within each, so seq is
deterministic. The module is the docLink's page path (`./Mathlib/A/B.html#X` -> `Mathlib.A.B`).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE decl_names(seq INTEGER PRIMARY KEY, name TEXT NOT NULL, library TEXT NOT NULL,
                        module TEXT NOT NULL, doc_link TEXT NOT NULL, UNIQUE(name, library));
CREATE VIRTUAL TABLE decl_names_fts USING fts5(name, content='', tokenize='trigram');
"""


def doc_module(doc_link: str) -> str:
    """`./Mathlib/Algebra/Group/Basic.html#mul_one` -> `Mathlib.Algebra.Group.Basic`."""
    page = doc_link.split("#", 1)[0]
    if page.startswith("./"):
        page = page[2:]
    if page.endswith(".html"):
        page = page[: -len(".html")]
    return page.replace("/", ".")


def name_rows(indexes: dict[str, dict[str, dict]]):
    """(name, library, module, doc_link) for every declaration, library by library."""
    for library, decls in indexes.items():
        for name in sorted(decls):
            link = decls[name].get("docLink") or ""
            yield name, library, doc_module(link), link


def add_name_index(serving: Path, bmps: dict[str, Path]) -> dict:
    t0 = time.monotonic()
    indexes = {lib: json.loads(path.read_text(encoding="utf-8"))["declarations"]
               for lib, path in bmps.items()}
    before = serving.stat().st_size
    db = sqlite3.connect(serving)
    try:
        db.execute("DROP TABLE IF EXISTS decl_names")
        db.execute("DROP TABLE IF EXISTS decl_names_fts")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO decl_names VALUES (?, ?, ?, ?, ?)",
                       ((seq, *row) for seq, row in enumerate(name_rows(indexes), 1)))
        db.execute("INSERT INTO decl_names_fts(rowid, name) SELECT seq, name FROM decl_names")
        db.commit()
        rows = db.execute("SELECT count(*) FROM decl_names").fetchone()[0]
        by_lib = dict(db.execute("SELECT library, count(*) FROM decl_names GROUP BY library"))
        db.execute("VACUUM")
    finally:
        db.close()
    return {"rows": rows, "by_library": by_lib,
            "distinct_names": len(set().union(*(set(d) for d in indexes.values()))),
            "file_bytes_before": before, "file_bytes_after": serving.stat().st_size,
            "seconds": round(time.monotonic() - t0, 1)}
