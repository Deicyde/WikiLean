#!/usr/bin/env python3
"""build_serving_db turns a cell-asset tree into the serving database the D1 import reads."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_serving_db as bsd  # noqa: E402

SHARDS = {
    "ce": {"cell:Q1": {"id": "cell:Q1", "label": "Compact operator", "organs": [{"id": "Q1"}],
                       "syn": [{"to": "cell:decl:TauCeti:Spectrum.compact", "w": 2}]}},
    "cell_decl_tauceti": {
        "cell:decl:TauCeti:Spectrum.compact": {"label": "Spectrum.compact", "xy": [1.5, -2.25]},
        "cell:decl:TauCeti:StronglyContinuousSemigroup.mk": {"label": "StronglyContinuousSemigroup.mk"},
    },
}
LABELS = [
    {"id": "cell:Q1", "label": "Compact operator", "f": 3, "aka": ["Compact linear operator", "compact"],
     "p": "path:Mathlib/Analysis"},
    {"id": "cell:decl:TauCeti:StronglyContinuousSemigroup.mk", "label": "StronglyContinuousSemigroup.mk",
     "p": "path:TauCeti/Analysis/Semigroup"},
    {"id": "cell:decl:TauCeti:Spectrum.compact", "label": "Spectrum.compact"},
]
ALIASES = {"_meta": {"counts": {}},
           "organs": {"Q1": "cell:Q1", "decl:TauCeti:Spectrum.compact": "cell:decl:TauCeti:Spectrum.compact"},
           "decls": {"Spectrum.compact": "cell:decl:TauCeti:Spectrum.compact"},
           "slugs": {"compact-operator": "cell:Q1"}}
SUPERCELLS = {"_meta": {}, "roots": ["path:TauCeti"],
              "supercells": {"path:TauCeti": {"label": "TauCeti", "children": ["path:TauCeti/Analysis"]},
                             "path:TauCeti/Analysis": {"label": "Analysis", "cells": ["cell:decl:TauCeti:Spectrum.compact"]}}}
NODES = [
    {"_meta": {"schema": "brain/SCHEMA.md"}},
    {"id": "Q1", "label": "Compact operator", "description": "operator mapping bounded sets to relatively compact sets"},
    {"id": "decl:TauCeti:Spectrum.compact", "label": "Spectrum.compact",
     "docstring": "The spectrum of a compact operator is countable."},
    {"id": "Q2", "label": "No text here"},
]


class ServingDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cells = self.tmp / "cells"
        cells.mkdir()
        manifest = {"_meta": {"generated_at": "2026-11-07T00:00:00Z"},
                    "shards": {key: len(rows) for key, rows in SHARDS.items()}}
        for name, doc in (("manifest.json", manifest), ("labels.json", LABELS),
                          ("aliases.json", ALIASES), ("supercells.json", SUPERCELLS),
                          ("explorer.json", {"nodes": []})):
            (cells / name).write_text(json.dumps(doc))
        for key, rows in SHARDS.items():
            (cells / f"{key}.json").write_text(json.dumps(rows))
        self.nodes = self.tmp / "nodes.jsonl"
        self.nodes.write_text("".join(json.dumps(n) + "\n" for n in NODES))
        self.cells = cells

    def build(self, name="serving.sqlite3"):
        out = self.tmp / name
        tables = bsd.build(self.cells, self.nodes, out)
        return tables, sqlite3.connect(out)

    def test_cell_pages_keep_shard_bytes(self):
        _, db = self.build()
        rows = dict(db.execute("SELECT id, json FROM cell_pages"))
        expected = {cid: entry for shard in SHARDS.values() for cid, entry in shard.items()}
        self.assertEqual(sorted(rows), sorted(expected))
        for cid, entry in expected.items():
            self.assertEqual(rows[cid], json.dumps(entry, ensure_ascii=False, separators=(",", ":")))

    def test_aliases_labels_and_supercells(self):
        _, db = self.build()
        self.assertEqual(db.execute("SELECT owner FROM aliases WHERE kind = 'decl' AND key = 'Spectrum.compact'").fetchone(),
                         ("cell:decl:TauCeti:Spectrum.compact",))
        self.assertEqual(db.execute("SELECT count(*) FROM aliases").fetchone(), (4,))
        self.assertEqual([r[0] for r in db.execute("SELECT id FROM labels ORDER BY ord")], [r["id"] for r in LABELS])
        self.assertEqual(db.execute("SELECT aka, f, p FROM labels WHERE ord = 0").fetchone(),
                         ("Compact linear operator\x1fcompact", 3, "path:Mathlib/Analysis"))
        self.assertEqual(json.loads(db.execute("SELECT json FROM supercells WHERE path = 'path:TauCeti/Analysis'").fetchone()[0]),
                         SUPERCELLS["supercells"]["path:TauCeti/Analysis"])

    def test_name_fragments_and_docstring_words_are_searchable(self):
        _, db = self.build()
        hits = db.execute("SELECT l.id FROM names JOIN labels l ON l.ord = names.rowid "
                          "WHERE names MATCH ? ORDER BY l.ord", ('"Semigroup"',)).fetchall()
        self.assertEqual(hits, [("cell:decl:TauCeti:StronglyContinuousSemigroup.mk",)])
        aka = db.execute("SELECT rowid FROM names WHERE names MATCH ?", ('"linear oper"',)).fetchall()
        self.assertEqual(aka, [(0,)])
        texts = db.execute("SELECT id FROM texts WHERE texts MATCH ? ORDER BY bm25(texts)",
                           ("spectrum countable",)).fetchall()
        self.assertEqual(texts, [("decl:TauCeti:Spectrum.compact",)])
        self.assertEqual(db.execute("SELECT count(*) FROM texts").fetchone(), (2,))

    def test_digests_are_recorded_and_reproducible(self):
        first, db = self.build("a.sqlite3")
        second, _ = self.build("b.sqlite3")
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"cell_pages", "aliases", "labels", "supercells"})
        recorded = {n: {"rows": r, "digest": d} for n, r, d in db.execute("SELECT name, rows, digest FROM tables")}
        self.assertEqual(recorded, first)
        self.assertEqual(first["cell_pages"]["rows"], 3)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["schema_version"], str(bsd.SCHEMA_VERSION))
        self.assertEqual(sorted(json.loads(meta["inputs"])), sorted([*bsd.WHOLE_FILES, "nodes.jsonl"]))

    def test_publishes_atomically(self):
        self.build()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")), [])
        (self.cells / "labels.json").write_text("[not json")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")), [])
        # the earlier build is still in place
        self.assertEqual(sqlite3.connect(self.tmp / "serving.sqlite3").execute(
            "SELECT count(*) FROM cell_pages").fetchone(), (3,))


if __name__ == "__main__":
    unittest.main()
