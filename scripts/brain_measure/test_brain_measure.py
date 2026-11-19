#!/usr/bin/env python3
"""Offline tests of the step-0 measurement harness's own logic (no builds, no inputs)."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import measure  # noqa: E402
import names  # noqa: E402


class SelectTest(unittest.TestCase):
    def test_trough_names_are_the_mapped_keys_and_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "deps.json").write_text(json.dumps({
                "declarations": {"A": {"statement": ["S"], "proof": ["P"]}}, "missing": ["M"]}))
            (t / "selection.json").write_text(json.dumps({
                "chosen": {"C": "named theorem"}, "assuming": {"D": {"statement": "E"}}}))
            (t / "roadmaps.json").write_text(json.dumps({"roadmaps": [{"names": ["R1", "R2"]}]}))
            got = measure.trough_names(t)
        self.assertEqual(got, {"deps.json": {"A", "M"}, "selection.json": {"C", "D"},
                               "roadmaps.json": {"R1", "R2"}})

    def test_plan_names_prefers_resolved_name_and_keeps_tauceti_only(self):
        rows = [{"resolution": "tauceti", "name": "X.y", "resolved_name": "TauCeti.X.y"},
                {"resolution": "tauceti", "name": "TauCeti.Z"},
                {"resolution": "mathlib", "name": "Nat.succ"},
                {"resolution": "planned", "name": "TauCeti.Later"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decl_refs.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(measure.plan_names(path), {"TauCeti.X.y", "TauCeti.Z"})

    def test_hybrid_union_keeps_harvested_names_and_counts_sources(self):
        result = measure.hybrid_union({"a": {"x", "y"}, "b": {"y", "z"}}, {"x", "y"})
        self.assertEqual(result["kept"], {"x", "y"})
        self.assertEqual(result["counts"]["union"], 3)
        self.assertEqual(result["counts"]["not_harvested"], 1)
        self.assertEqual(result["counts"]["kept:b"], 1)


class GraphTest(unittest.TestCase):
    def test_strip_layer_removes_tauceti_nodes_and_their_edges(self):
        nodes = [{"id": "decl:TauCeti:a", "type": "decl"},
                 {"id": "path:TauCeti", "type": "container", "library": "TauCeti"},
                 {"id": "decl:Mathlib:b", "type": "decl"},
                 {"id": "path:Mathlib", "type": "container", "library": "Mathlib"}]
        edges = [{"src": "path:TauCeti", "dst": "decl:TauCeti:a", "provenance": {"source": "tauceti"}},
                 {"src": "decl:TauCeti:a", "dst": "decl:Mathlib:b", "provenance": {"source": "x"}},
                 {"src": "path:Mathlib", "dst": "decl:Mathlib:b", "provenance": {"source": "mathlib"}}]
        kept_nodes, kept_edges, stats = measure.strip_layer(nodes, edges)
        self.assertEqual([n["id"] for n in kept_nodes], ["decl:Mathlib:b", "path:Mathlib"])
        self.assertEqual(len(kept_edges), 1)
        self.assertEqual(stats, {"nodes_removed": 2, "edges_removed": 2})

    def test_mathlib_universe_drops_ambiguous_dotless_names(self):
        nodes = [{"id": "decl:Mathlib:Module.Basis", "label": "Module.Basis"},
                 {"id": "decl:Mathlib:Basis", "label": "Basis"},
                 {"id": "decl:Mathlib:Nat", "label": "Nat"},
                 {"id": "decl:TauCeti:Foo", "label": "Foo"}]
        self.assertEqual(measure.mathlib_universe(nodes),
                         {"Module.Basis": "decl:Mathlib:Module.Basis", "Nat": "decl:Mathlib:Nat"})


class NamesTest(unittest.TestCase):
    def test_doc_module(self):
        self.assertEqual(names.doc_module("./Mathlib/Algebra/Group/Basic.html#mul_one"),
                         "Mathlib.Algebra.Group.Basic")
        self.assertEqual(names.doc_module("./Init/Core.html"), "Init.Core")

    def test_add_name_index_builds_rows_and_trigram_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            serving = t / "serving.sqlite3"
            sqlite3.connect(serving).close()
            (t / "m.bmp").write_text(json.dumps({"declarations": {
                "mul_comm": {"docLink": "./Mathlib/Algebra/Group/Defs.html#mul_comm"},
                "Nat.succ": {"docLink": "./Init/Prelude.html#Nat.succ"}}}))
            (t / "t.bmp").write_text(json.dumps({"declarations": {
                "mul_comm": {"docLink": "./Mathlib/Algebra/Group/Defs.html#mul_comm"},
                "TauCeti.Hopf.antipode": {"docLink": "./TauCeti/Hopf.html#TauCeti.Hopf.antipode"}}}))
            facts = names.add_name_index(serving, {"Mathlib": t / "m.bmp", "TauCeti": t / "t.bmp"})
            self.assertEqual((facts["rows"], facts["distinct_names"]), (4, 3))
            db = sqlite3.connect(serving)
            self.assertEqual(db.execute("SELECT seq, name, library, module FROM decl_names "
                                        "ORDER BY seq").fetchall(),
                             [(1, "Nat.succ", "Mathlib", "Init.Prelude"),
                              (2, "mul_comm", "Mathlib", "Mathlib.Algebra.Group.Defs"),
                              (3, "TauCeti.Hopf.antipode", "TauCeti", "TauCeti.Hopf"),
                              (4, "mul_comm", "TauCeti", "Mathlib.Algebra.Group.Defs")])
            hits = db.execute("SELECT rowid FROM decl_names_fts WHERE decl_names_fts MATCH ? "
                              "ORDER BY rowid", (analyze.fts_phrase("ul_co"),)).fetchall()
            self.assertEqual(hits, [(2,), (4,)])
            db.close()


class AnalyzeTest(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        self.assertEqual(analyze.percentile([], 99), 0)
        self.assertEqual(analyze.percentile(list(range(1, 101)), 99), 99)
        self.assertEqual(analyze.percentile([5], 50), 5)
        self.assertEqual(analyze.percentile([3, 1, 2], 50), 2)

    def test_plan_rows_stays_in_budget_and_covers_every_row(self):
        rows = [(i, f"name'{i}" * (i % 7 + 1), "Lib") for i in range(1, 301)]
        budget = 400
        statements = list(analyze.plan_rows("t", ["seq", "name", "lib"], rows, budget, ds=3))
        self.assertTrue(all(len(s.encode()) <= budget for s in statements))
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t(ds INTEGER, seq INTEGER, name TEXT, lib TEXT, UNIQUE(ds, seq))")
        for statement in statements:
            db.execute(statement)
        for statement in statements:            # a rerun replaces every range, as the importer's does
            db.execute(statement)
        self.assertEqual(db.execute("SELECT seq, name, lib FROM t WHERE ds = 3 ORDER BY seq").fetchall(),
                         rows)
        self.assertEqual(sum(s.startswith("DELETE") for s in statements) * 2, len(statements))

    def test_plan_rows_refuses_a_row_over_budget(self):
        with self.assertRaises(ValueError):
            list(analyze.plan_rows("t", ["seq", "name"], [(1, "x" * 500)], 200))

    def test_group_fts_folds_shadow_tables(self):
        stat = {"names_data": 10, "names_idx": 2, "names_docsize": 3, "labels": 7,
                "sqlite_autoindex_labels_1": 1, "names_of_things": 4}
        self.assertEqual(analyze.group_fts(stat, ["names"]),
                         {"names (fts)": 15, "labels": 7, "names_of_things": 4,
                          "sqlite_autoindex_labels_1": 1})

    def test_fts_phrase_quotes(self):
        self.assertEqual(analyze.fts_phrase('a"b'), '"a""b"')


class NameSearchTest(unittest.TestCase):
    """A hot prefix must still time the read its row replaces, not only a count of it."""

    def names_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.executescript(
            "CREATE TABLE name_keys(seq INTEGER PRIMARY KEY, name_lc TEXT NOT NULL, owner TEXT);"
            "CREATE INDEX name_keys_lc ON name_keys(name_lc);"
            "CREATE TABLE hot_prefixes(prefix TEXT PRIMARY KEY, cells TEXT, sups TEXT, live_sups TEXT);"
            "CREATE VIRTUAL TABLE names USING fts5(name, content='', tokenize='trigram');")
        db.executemany("INSERT INTO name_keys VALUES (?, ?, ?)",
                       ((i, f"tauceti.d{i:05}", f"cell:{i}") for i in range(1, 2501)))
        db.execute("INSERT INTO hot_prefixes VALUES ('tau', '[]', '[]', '[]')")
        return db

    def test_a_hot_prefix_times_the_bounded_read_it_replaces(self):
        db = self.names_db()
        got = analyze.name_search(db, "tau")
        self.assertTrue(got["hot"])
        # the read the Worker would run without the hot row: the same bounded statement the
        # non-hot branch times, capped at 1001 rows, NOT the 2,500-row count beside it
        self.assertEqual(got["prefix_range_if_not_hot"]["rows"], 1001)
        self.assertEqual(got["prefix_range_count_if_not_hot"]["count"], 2500)
        self.assertEqual(got["prefix_range_count_if_not_hot"]["rows"], 1)
        db.close()

    def test_a_cold_prefix_times_the_same_statement(self):
        db = self.names_db()
        got = analyze.name_search(db, "tauceti.d000")
        self.assertFalse(got["hot"])
        self.assertNotIn("prefix_range_if_not_hot", got)
        self.assertEqual(got["prefix_range"]["rows"], 99)   # d00001..d00099
        db.close()


if __name__ == "__main__":
    unittest.main()
