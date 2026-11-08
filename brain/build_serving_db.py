#!/usr/bin/env python3
"""Build the Brain's serving database: what the Worker reads from D1, as one SQLite file.

    python3 brain/build_serving_db.py [--cells-dir DIR] [--nodes PATH] [--xref PATH]
                                      [--out PATH] [--sidecar PATH]

Reads a built cell-asset tree (build_cell_shards.py's output: the shards and trace buckets its
manifest lists, plus aliases.json, labels.json and supercells.json), site/assets/brain/
xref_index.json and brain/data/nodes.jsonl. Writes brain/data/serving.sqlite3 and its sidecar
brain/data/serving-tables.json, published together. Neither carries a release id or a generation
stamp: the importer adds `ds` when it loads the rows into the shared `wikilean-brain` database,
and the sidecar's logical root names the dataset (docs: the D1 read-layer design, section 4).

Tables (each numbers its rows seq = 1..N in its key order, the order its digest reads them)
  cell_pages(id, json)            each cell's shard entry, serialized as build_cell_shards does
  aliases(kind, key, owner)       kind: organ, decl or slug (aliases.json's three maps)
  labels(ord, id, label, aka, f, p, libs, xy)
                                  labels.json in its order; `aka` and `libs` joined with U+001F
  supercells(ord, path, label, aka, parent, fa, frontier, n_cells, n_sub, fhist, kids, syn, json)
                                  supercells.json in file order, plus subtree counts
  name_keys(name_lc, kind, owner, len16, name)
                                  every searchable name, lowercased, once per (name, kind, owner)
  hot_prefixes(prefix, cells, sups)   every prefix that starts more than 1,000 names
  cell_under(anc, ord)            every `/`-prefix of every folder that lists a cell
  synapse_traces(pair, json)      the trace sidecar's rows
  xrefs(page_ord, node_ord, page, node)   xref_index.json in file order
  text_ids(node, owner, body)     node label, docstring and description, in owner order
  names, texts                    FTS5 over name_keys.name (trigram) and text_ids.body
  tables(name, rows, digest, delete_bounds)   what the sidecar says about each table
  meta(key, value)                schema version and the inputs' digests

The SQLite bytes depend on the SQLite version; the logical digests (store.digest_rows over every
column of a table in seq order) do not, and are what the sidecar binds. Numbers live only in
INTEGER columns and floats stay inside JSON text, so rows read back from D1 digest the same.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from store import canonical_json, digest_file, digest_rows

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
SIDECAR_SCHEMA = "wikilean.brain-serving-tables/v1"
AKA_SEP = "\x1f"
WHOLE_FILES = ("aliases.json", "labels.json", "supercells.json", "manifest.json")
ALIAS_KINDS = {"organs": "organ", "decls": "decl", "slugs": "slug"}
HOT_PREFIX_NAMES = 1000      # a prefix starting more names than this is listed in hot_prefixes
HOT_PREFIX_OWNERS = 100      # owners listed per hot prefix, for cells and supercells each
QUERY_UNITS = (2, 200)       # the Worker's shortest and longest search query, UTF-16 units
KIDS_CAP = 64                # children carried on a supercell row, the largest by n_sub
DELETE_BOUND_BYTES = 4 * 2**20   # the pruner deletes about this much row data per statement

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tables(name TEXT PRIMARY KEY, rows INTEGER NOT NULL, digest TEXT NOT NULL,
                    delete_bounds TEXT NOT NULL);
CREATE TABLE cell_pages(seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, json TEXT NOT NULL);
CREATE TABLE aliases(seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
                     owner TEXT NOT NULL, UNIQUE(kind, key));
CREATE TABLE labels(seq INTEGER PRIMARY KEY, ord INTEGER NOT NULL UNIQUE, id TEXT NOT NULL UNIQUE,
                    label TEXT, aka TEXT, f INTEGER, p TEXT, libs TEXT, xy TEXT);
CREATE INDEX labels_p ON labels(p);
CREATE TABLE supercells(seq INTEGER PRIMARY KEY, ord INTEGER NOT NULL UNIQUE,
                        path TEXT NOT NULL UNIQUE, label TEXT, aka TEXT, parent TEXT, fa INTEGER,
                        frontier INTEGER, n_cells INTEGER, n_sub INTEGER NOT NULL,
                        fhist TEXT NOT NULL, kids TEXT, syn TEXT, json TEXT NOT NULL);
CREATE INDEX supercells_parent ON supercells(parent, ord);
CREATE TABLE name_keys(seq INTEGER PRIMARY KEY, name_lc TEXT NOT NULL, kind TEXT NOT NULL,
                       owner TEXT NOT NULL, len16 INTEGER NOT NULL, name TEXT NOT NULL,
                       UNIQUE(name_lc, kind, owner));
CREATE TABLE hot_prefixes(seq INTEGER PRIMARY KEY, prefix TEXT NOT NULL UNIQUE,
                          cells TEXT NOT NULL, sups TEXT NOT NULL);
CREATE TABLE cell_under(seq INTEGER PRIMARY KEY, anc TEXT NOT NULL, ord INTEGER NOT NULL,
                        UNIQUE(anc, ord));
CREATE TABLE synapse_traces(seq INTEGER PRIMARY KEY, pair TEXT NOT NULL UNIQUE,
                            json TEXT NOT NULL);
CREATE TABLE xrefs(seq INTEGER PRIMARY KEY, page_ord INTEGER NOT NULL, node_ord INTEGER NOT NULL,
                   page TEXT NOT NULL, node TEXT NOT NULL, UNIQUE(page_ord, node_ord));
CREATE INDEX xrefs_node ON xrefs(node, page_ord);
CREATE INDEX xrefs_page ON xrefs(page, node_ord);
CREATE TABLE text_ids(seq INTEGER PRIMARY KEY, node TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
                      body TEXT NOT NULL);
CREATE VIRTUAL TABLE names USING fts5(name, content='', contentless_delete=1, tokenize='trigram');
CREATE VIRTUAL TABLE texts USING fts5(body, content='', contentless_delete=1);
"""
# the tables the importer copies, each digested over every column in seq order
DATA_TABLES = ("cell_pages", "aliases", "labels", "supercells", "name_keys", "hot_prefixes",
               "cell_under", "synapse_traces", "xrefs", "text_ids")


def compact(value) -> str:
    """The serialization build_cell_shards gives a shard entry (key order kept)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, as JavaScript's String.length counts it."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def fill(db: sqlite3.Connection, table: str, rows, *, order_by: tuple[str, ...] = (),
         sort_keys: int = 0) -> None:
    """Insert `rows`, numbering them seq = 1..N.

    Without `order_by` the rows already come in the table's key order. With it, SQLite sorts
    them (through a temporary table, so a large table is never sorted in Python's memory);
    each row then starts with `sort_keys` extra columns k0, k1, ... that `order_by` may name."""
    columns = [c[0] for c in db.execute(f"SELECT * FROM {table} LIMIT 0").description][1:]
    if not order_by:
        db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * (len(columns) + 1))})",
                       ((seq, *row) for seq, row in enumerate(rows, 1)))
        return
    staged = [f"k{i}" for i in range(sort_keys)] + columns
    db.execute(f"CREATE TEMP TABLE stage({', '.join(staged)})")
    db.executemany(f"INSERT INTO stage VALUES ({', '.join('?' * len(staged))})", rows)
    order = ", ".join(order_by)
    db.execute(f"INSERT INTO {table} SELECT row_number() OVER (ORDER BY {order}), "
               f"{', '.join(columns)} FROM stage ORDER BY {order}")
    db.execute("DROP TABLE stage")


def node_texts(nodes_path: Path):
    """(line, id, body) for every node with a docstring or description."""
    with nodes_path.open(encoding="utf-8") as handle:
        next(handle)                                   # the _meta line
        for line_no, line in enumerate(handle):
            node = json.loads(line)
            if node.get("docstring") or node.get("description"):
                parts = (node.get("label"), node.get("docstring"), node.get("description"))
                yield line_no, node["id"], " ".join(str(p) for p in parts if p)


def slash_prefixes(path: str) -> list[str]:
    """`path` and each prefix of it that ends just before a '/': exactly the values P for which
    the Worker's under test inPrefix(path) holds (path == P or path starts with P + '/')."""
    return [path[:i] for i, ch in enumerate(path) if ch == "/" and i] + [path]


def subtree(labels: list[dict], sup_doc: dict):
    """Each label row's libs, and each supercell's n_sub and fhist.

    A cell's folders are the supercells whose `cells` list holds it, frontier areas included.
    Its libs are the library roots above those folders (what the page's cellLibs computes,
    build_brain_page.py:buildCellLibs); n_sub counts it once per ancestor, as
    build_cell_shards' subtree_cells does, and fhist splits that count by "<f>|<libs>"."""
    supercells = sup_doc["supercells"]
    folders: dict[str, list[str]] = defaultdict(list)
    for path, row in supercells.items():
        for cid in row.get("cells") or []:
            folders[cid].append(path)
    chains: dict[str, list[str]] = {}

    def chain(path: str) -> list[str]:
        """`path` and its ancestors, stopping at a cycle as build_cell_shards does."""
        if path not in chains:
            seen, cur = [], path
            while cur is not None and cur not in seen:
                seen.append(cur)
                cur = (supercells.get(cur) or {}).get("parent")
            chains[path] = seen
        return chains[path]

    libraries = {p for p in sup_doc.get("roots") or [] if not supercells.get(p, {}).get("frontier")}
    libs: list[str | None] = []
    n_sub: Counter = Counter()
    fhist: dict[str, Counter] = defaultdict(Counter)
    for row in labels:
        ancestors: set[str] = set()
        roots: set[str] = set()
        for folder in folders.get(row["id"], ()):
            up = chain(folder)
            ancestors.update(up)
            if up[-1] in libraries:
                roots.add(up[-1][len("path:"):])
        joined = AKA_SEP.join(sorted(roots)) or None
        libs.append(joined)
        key = f"{row.get('f') or 0}|{joined or ''}"
        for path in ancestors:
            n_sub[path] += 1
            fhist[path][key] += 1
    return libs, n_sub, fhist, folders


def supercell_rows(supercells: dict, n_sub: Counter, fhist: dict):
    """supercells rows in file order: the sorted path: rows, then the frontier: rows."""
    for ord_, (path, row) in enumerate(supercells.items()):
        organs = row.get("organs") or []
        kids = sorted(row.get("children") or [], key=lambda child: (-n_sub[child], child))
        yield (ord_, path, row.get("label"),
               AKA_SEP.join(str(o.get("label") or o.get("id")) for o in organs) or None,
               row.get("parent"), row.get("fa"), 1 if row.get("frontier") else None,
               len(row.get("cells") or []) or None, n_sub[path],
               canonical_json(dict(fhist.get(path, {}))),
               canonical_json([{"path": child, "n_sub": n_sub[child],
                                "fhist": dict(fhist.get(child, {}))}
                               for child in kids[:KIDS_CAP]]) if kids else None,
               compact(row["syn"]) if row.get("syn") else None,
               compact(row))


def search_path(path: str) -> str:
    """The whole path after `path:`, or the area name after `frontier:`: the page's area hay."""
    for prefix in ("path:", "frontier:"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def name_rows(labels: list[dict], supercells: dict):
    """name_keys rows in seq order: cells in labels order (label, then aka), then supercells in
    file order (label, whole path, organ labels). One row per (lowercased name, kind, owner),
    keeping the first spelling, since aka is only deduplicated by exact string."""
    def owners():
        for row in labels:
            yield row["id"], [("label", row.get("label"))] + [("aka", a) for a in row.get("aka") or []]
        for path, row in supercells.items():
            yield path, ([("slabel", row.get("label")), ("spath", search_path(path))]
                         + [("sorgan", o.get("label")) for o in row.get("organs") or []])

    for owner, names in owners():
        seen = set()
        for kind, name in names:
            if not name:
                continue
            name_lc = str(name).lower()
            if (name_lc, kind) not in seen:
                seen.add((name_lc, kind))
                yield name_lc, kind, owner, utf16_len(name_lc), str(name)


def hot_prefix_rows(keys: list[tuple[int, str, str, bool]]):
    """hot_prefixes rows in prefix order. `keys` is (seq, name_lc, owner, is_supercell) in seq
    order. A prefix is hot when more than HOT_PREFIX_NAMES names start with it; the Worker
    reads every other prefix as an index range of at most that many rows. Each hot prefix
    lists the first name_keys.seq of its first HOT_PREFIX_OWNERS cell owners and, apart, of
    its first HOT_PREFIX_OWNERS supercell owners."""
    hot = []
    groups = [("", keys)]
    while groups:
        prefix, members = groups.pop()
        depth = len(prefix) + 1
        children: dict[str, list] = defaultdict(list)
        for key in members:
            if len(key[1]) >= depth:
                children[key[1][:depth]].append(key)
        for child, sub in children.items():
            if len(sub) <= HOT_PREFIX_NAMES:
                continue
            units = utf16_len(child)
            if QUERY_UNITS[0] <= units <= QUERY_UNITS[1]:
                hot.append((child, sub))
            if units < QUERY_UNITS[1]:
                groups.append((child, sub))
    for prefix, sub in sorted(hot, key=lambda item: item[0]):
        lists: tuple[list[int], list[int]] = ([], [])
        seen: tuple[set, set] = (set(), set())
        for seq, _, owner, is_sup in sub:
            if owner not in seen[is_sup] and len(lists[is_sup]) < HOT_PREFIX_OWNERS:
                seen[is_sup].add(owner)
                lists[is_sup].append(seq)
        yield prefix, compact(lists[0]), compact(lists[1])


def digest_table(db: sqlite3.Connection, table: str) -> dict:
    """A table's row count, digest over every column in seq order, and delete bounds: the seq
    values at which the cumulative row bytes cross each multiple of DELETE_BOUND_BYTES."""
    cursor = db.execute(f"SELECT * FROM {table} ORDER BY seq")
    columns = [c[0] for c in cursor.description]
    state = {"rows": 0, "bytes": 0, "next": DELETE_BOUND_BYTES}
    bounds: list[int] = []

    def rows():
        for values in cursor:
            state["rows"] += 1
            if values[0] != state["rows"]:
                raise ValueError(f"{table}: seq {values[0]} where {state['rows']} belongs")
            if any(isinstance(v, float) for v in values):
                raise ValueError(f"{table} seq {values[0]} holds a REAL: floats belong in JSON text")
            row = dict(zip(columns, values))
            state["bytes"] += len(canonical_json(row).encode("utf-8")) + 1
            if state["bytes"] >= state["next"]:
                bounds.append(values[0])
                state["next"] = (state["bytes"] // DELETE_BOUND_BYTES + 1) * DELETE_BOUND_BYTES
            yield row

    digest = digest_rows(rows())
    return {"rows": state["rows"], "digest": digest, "delete_bounds": bounds}


def publish_together(pairs: list[tuple[Path, Path]]) -> None:
    """Rename each finished temp file over its destination, all or none.

    If a rename fails, the destinations already replaced get their old bytes back (from hard
    links taken first), so a reader never pairs a new serving file with an old sidecar. Only a
    crash between the renames can leave a mixed pair, and the sidecar's digests expose it."""
    backups: list[tuple[Path, Path | None]] = []
    try:
        for temp, dest in pairs:
            backup = None
            if dest.exists():
                backup = temp.with_suffix(".old")
                os.link(dest, backup)
            backups.append((dest, backup))
            os.replace(temp, dest)
    except BaseException:
        for dest, backup in reversed(backups):
            if backup is not None:
                os.replace(backup, dest)
            else:
                dest.unlink(missing_ok=True)
        raise
    finally:
        for _, backup in backups:
            if backup is not None:
                backup.unlink(missing_ok=True)


def build(cells_dir: Path, nodes_path: Path, out: Path, *, xref_path: Path | None = None,
          sidecar_path: Path | None = None) -> dict:
    xref_path = xref_path or cells_dir.parent / "xref_index.json"
    sidecar_path = sidecar_path or out.with_name("serving-tables.json")
    manifest = read_json(cells_dir / "manifest.json")
    traces = manifest.get("traces") or {}
    if "prov" in traces:
        # build_cell_shards ships traces.prov only when the synapse and cell prov tables differ
        raise ValueError(f"{cells_dir / 'manifest.json'}: traces carry their own prov table; "
                         "trace prov indexes must resolve against the manifest's")
    temps = []
    for dest in (out, sidecar_path):
        fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        os.close(fd)
        temps.append(Path(name))
    try:
        db = sqlite3.connect(temps[0])
        db.executescript(SCHEMA)
        pages = {}
        for key in sorted(manifest["shards"]):
            pages.update(read_json(cells_dir / f"{key}.json"))
        fill(db, "cell_pages", ((cid, compact(pages[cid])) for cid in sorted(pages)))
        aliases = read_json(cells_dir / "aliases.json")
        fill(db, "aliases", ((kind, key, aliases[field][key])
                             for field, kind in sorted(ALIAS_KINDS.items(), key=lambda fk: fk[1])
                             for key in sorted(aliases[field])))

        labels = read_json(cells_dir / "labels.json")
        sup_doc = read_json(cells_dir / "supercells.json")
        supercells = sup_doc["supercells"]
        libs, n_sub, fhist, folders = subtree(labels, sup_doc)

        def xy(cid):
            value = ((pages.get(cid) or {}).get("cell") or {}).get("xy")
            return None if value is None else compact(value)
        fill(db, "labels", ((i, r["id"], r.get("label"),
                             AKA_SEP.join(r["aka"]) if r.get("aka") else None,
                             r.get("f"), r.get("p"), libs[i], xy(r["id"]))
                            for i, r in enumerate(labels)))
        del pages
        fill(db, "supercells", supercell_rows(supercells, n_sub, fhist))

        fill(db, "name_keys", name_rows(labels, supercells))
        sup_names_from = 1 + db.execute(
            "SELECT count(*) FROM name_keys WHERE kind IN ('label', 'aka')").fetchone()[0]
        keys = [(seq, name_lc, owner, seq >= sup_names_from)
                for seq, name_lc, owner in db.execute("SELECT seq, name_lc, owner FROM name_keys ORDER BY seq")]
        fill(db, "hot_prefixes", hot_prefix_rows(keys))
        del keys

        def under():
            # p is always one of the cell's folders today; taking it as well keeps the
            # table equal to the Worker's union of both signals even if that drifts
            prefixes: dict[str, list[str]] = {}
            for i, r in enumerate(labels):
                ancestors = set()
                for folder in folders.get(r["id"], []) + ([r["p"]] if r.get("p") else []):
                    if folder not in prefixes:
                        prefixes[folder] = slash_prefixes(folder)
                    ancestors.update(prefixes[folder])
                for anc in ancestors:
                    yield anc, i
        fill(db, "cell_under", under(), order_by=("anc", "ord"))

        trace_dir = cells_dir / traces.get("dir", "traces")
        fill(db, "synapse_traces", ((pair, compact(value))
                                    for key in sorted(traces.get("files") or {})
                                    for pair, value in read_json(trace_dir / f"{key}.json").items()),
             order_by=("pair",))
        xref = read_json(xref_path)
        fill(db, "xrefs", ((page_ord, node_ord, page, node)
                           for page_ord, (page, nodes) in enumerate(xref.items())
                           for node_ord, node in enumerate(nodes)))

        # text hits rank richer atoms first: owners in labels order, then supercells in
        # file order; a node's owner is the atom its organ id resolves to
        rank = {r["id"]: i for i, r in enumerate(labels)}
        rank.update((path, len(labels) + i) for i, path in enumerate(supercells))
        organs = aliases["organs"]
        fill(db, "text_ids", ((rank[owner], line_no, node, owner, body)
                              for line_no, node, body in node_texts(nodes_path)
                              for owner in [organs.get(node) or (node if node in supercells else None)]
                              if owner in rank),
             order_by=("k0", "k1"), sort_keys=2)

        db.execute("INSERT INTO names(rowid, name) SELECT seq, name FROM name_keys")
        db.execute("INSERT INTO texts(rowid, body) SELECT seq, body FROM text_ids")
        tables = {name: digest_table(db, name) for name in DATA_TABLES}
        db.executemany("INSERT INTO tables VALUES (?, ?, ?, ?)",
                       ((n, t["rows"], t["digest"], compact(t["delete_bounds"]))
                        for n, t in tables.items()))
        longest_name = db.execute("SELECT coalesce(max(len16), 0) FROM name_keys "
                                  "WHERE kind IN ('label', 'aka')").fetchone()[0]
        inputs = {name: digest_file(cells_dir / name) for name in WHOLE_FILES}
        inputs["nodes.jsonl"] = digest_file(nodes_path)
        inputs["xref_index.json"] = digest_file(xref_path)
        db.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("schema_version", str(SCHEMA_VERSION)),
            ("cells_generated_at", str(manifest.get("_meta", {}).get("generated_at", ""))),
            ("inputs", canonical_json(inputs)),
        ])
        db.commit()
        db.execute("VACUUM")
        db.close()
        # the dataset's identity: no generation stamp and no release id, so identical tables
        # always give the identical sidecar
        sidecar = {"schema": SIDECAR_SCHEMA, "schema_version": SCHEMA_VERSION, "tables": tables,
                   "longest_name": longest_name, "sup_names_from": sup_names_from}
        temps[1].write_text(json.dumps(sidecar, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        publish_together(list(zip(temps, (out, sidecar_path))))
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
    return sidecar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cells-dir", type=Path, default=ROOT / "site" / "assets" / "brain" / "cells")
    parser.add_argument("--nodes", type=Path, default=ROOT / "brain" / "data" / "nodes.jsonl")
    parser.add_argument("--xref", type=Path, default=None,
                        help="xref_index.json (default: next to the cells directory)")
    parser.add_argument("--out", type=Path, default=ROOT / "brain" / "data" / "serving.sqlite3")
    parser.add_argument("--sidecar", type=Path, default=None,
                        help="serving-tables.json (default: next to --out)")
    args = parser.parse_args(argv)
    sidecar = build(args.cells_dir, args.nodes, args.out, xref_path=args.xref,
                    sidecar_path=args.sidecar)
    for name, table in sidecar["tables"].items():
        print(f"{name:14} {table['rows']:>10,} rows  {table['digest'][:12]}")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB) and its sidecar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
