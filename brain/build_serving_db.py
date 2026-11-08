#!/usr/bin/env python3
"""Build the Brain's serving database: what the Worker reads from D1, as one SQLite file.

    python3 brain/build_serving_db.py [--cells-dir DIR] [--nodes PATH] [--out PATH]

Reads a built cell-asset tree (build_cell_shards.py's output: the shards its manifest lists,
plus aliases.json, labels.json and supercells.json) and brain/data/nodes.jsonl, and writes
brain/data/serving.sqlite3. The file carries no release id: the importer adds it when it
loads the rows into the shared `wikilean-brain` database (docs: the TauCeti design note).

Tables
  cell_pages(id, json)            each cell's shard entry, serialized as build_cell_shards does
  aliases(kind, key, owner)       kind: organ, decl or slug (aliases.json's three maps)
  labels(ord, id, label, aka, f, p)   labels.json in its order; `aka` joined with U+001F
  supercells(path, json)          one supercells.json row per folder
  names                           FTS5 trigram over labels' label and aka: any fragment matches
  texts                           FTS5 word index over node labels, docstrings and descriptions
  tables(name, rows, digest)      each base table's row count and logical digest
  meta(key, value)                schema version and the inputs' digests

The SQLite bytes depend on the SQLite version; the logical digests (store.digest_rows over each
table's rows in key order) do not, and are what a release binds.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from store import canonical_json, digest_file, digest_rows

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
AKA_SEP = "\x1f"
WHOLE_FILES = ("aliases.json", "labels.json", "supercells.json", "manifest.json")
ALIAS_KINDS = {"organs": "organ", "decls": "decl", "slugs": "slug"}

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tables(name TEXT PRIMARY KEY, rows INTEGER NOT NULL, digest TEXT NOT NULL);
CREATE TABLE cell_pages(id TEXT NOT NULL UNIQUE, json TEXT NOT NULL);
CREATE TABLE aliases(kind TEXT NOT NULL, key TEXT NOT NULL, owner TEXT NOT NULL, UNIQUE(kind, key));
CREATE TABLE labels(ord INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, label TEXT,
                    aka TEXT, f INTEGER, p TEXT);
CREATE TABLE supercells(path TEXT NOT NULL UNIQUE, json TEXT NOT NULL);
CREATE VIRTUAL TABLE names USING fts5(label, aka, content='labels', content_rowid='ord',
                                      tokenize='trigram');
CREATE VIRTUAL TABLE texts USING fts5(id UNINDEXED, body);
"""
# each base table's rows in key order, as digested and as the importer copies them
TABLE_ROWS = {
    "cell_pages": "SELECT id, json FROM cell_pages ORDER BY id",
    "aliases": "SELECT kind, key, owner FROM aliases ORDER BY kind, key",
    "labels": "SELECT ord, id, label, aka, f, p FROM labels ORDER BY ord",
    "supercells": "SELECT path, json FROM supercells ORDER BY path",
}


def compact(value) -> str:
    """The serialization build_cell_shards gives a shard entry (key order kept)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def node_texts(nodes_path: Path):
    """(id, body) for every node with a docstring or description."""
    with nodes_path.open(encoding="utf-8") as handle:
        next(handle)                                   # the _meta line
        for line in handle:
            node = json.loads(line)
            if node.get("docstring") or node.get("description"):
                parts = (node.get("label"), node.get("docstring"), node.get("description"))
                yield node["id"], " ".join(str(p) for p in parts if p)


def build(cells_dir: Path, nodes_path: Path, out: Path) -> dict[str, dict]:
    manifest = read_json(cells_dir / "manifest.json")
    fd, temp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        db = sqlite3.connect(temp)
        db.executescript(SCHEMA)
        pages = {}
        for key in sorted(manifest["shards"]):
            pages.update(read_json(cells_dir / f"{key}.json"))
        db.executemany("INSERT INTO cell_pages VALUES (?, ?)",
                       ((cid, compact(pages[cid])) for cid in sorted(pages)))
        aliases = read_json(cells_dir / "aliases.json")
        db.executemany("INSERT INTO aliases VALUES (?, ?, ?)",
                       ((kind, key, aliases[field][key])
                        for field, kind in ALIAS_KINDS.items() for key in sorted(aliases[field])))
        labels = read_json(cells_dir / "labels.json")
        db.executemany("INSERT INTO labels VALUES (?, ?, ?, ?, ?, ?)",
                       ((i, r["id"], r.get("label"), AKA_SEP.join(r["aka"]) if r.get("aka") else None,
                         r.get("f"), r.get("p")) for i, r in enumerate(labels)))
        db.execute("INSERT INTO names(names) VALUES ('rebuild')")
        supercells = read_json(cells_dir / "supercells.json")["supercells"]
        db.executemany("INSERT INTO supercells VALUES (?, ?)",
                       ((path, compact(supercells[path])) for path in sorted(supercells)))
        db.executemany("INSERT INTO texts VALUES (?, ?)", node_texts(nodes_path))

        tables = {}
        for name, query in TABLE_ROWS.items():
            cursor = db.execute(query)
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor]
            tables[name] = {"rows": len(rows), "digest": digest_rows(rows)}
        db.executemany("INSERT INTO tables VALUES (?, ?, ?)",
                       ((n, t["rows"], t["digest"]) for n, t in tables.items()))
        inputs = {name: digest_file(cells_dir / name) for name in WHOLE_FILES}
        inputs["nodes.jsonl"] = digest_file(nodes_path)
        db.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("schema_version", str(SCHEMA_VERSION)),
            ("cells_generated_at", str(manifest.get("_meta", {}).get("generated_at", ""))),
            ("inputs", canonical_json(inputs)),
        ])
        db.commit()
        db.execute("VACUUM")
        db.close()
        os.replace(temp, out)
    finally:
        temp.unlink(missing_ok=True)
    return tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cells-dir", type=Path, default=ROOT / "site" / "assets" / "brain" / "cells")
    parser.add_argument("--nodes", type=Path, default=ROOT / "brain" / "data" / "nodes.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "brain" / "data" / "serving.sqlite3")
    args = parser.parse_args(argv)
    tables = build(args.cells_dir, args.nodes, args.out)
    for name, table in tables.items():
        print(f"{name:11} {table['rows']:>9,} rows  {table['digest'][:12]}")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
