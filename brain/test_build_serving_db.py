#!/usr/bin/env python3
"""build_serving_db turns a cell-asset tree into the serving database the D1 import reads."""
from __future__ import annotations

import bisect
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_cell_shards  # noqa: E402
import build_serving_db as bsd  # noqa: E402
from store import canonical_json, digest_rows  # noqa: E402
from test_store import PROV, make_fixture, write_jsonl  # noqa: E402

SPECTRUM = "cell:decl:TauCeti:Spectrum.compact"
SEMIGROUP = "cell:decl:TauCeti:StronglyContinuousSemigroup.mk"


def entry(label, xy, syn=()):
    return {"cell": {"label": label, "xy": xy}, "organs": [], "syn": list(syn)}


SHARDS = {
    # neither the shard's ids nor cell:Q1's keys are in sorted order, so a builder that
    # sorts keys, or numbers rows in file order, shows
    "ce": {"cell:Q8": entry("Unplaced thing", [3.0, 3.0]),
           "cell:Q1": {"syn": [{"w": 2, "id": SPECTRUM}], "cell": {"xy": [0.5, 1.0], "label": "Compact operator"},
                       "organs": []},
           "cell:Q5": entry("Cauchy–Schwarz inequality", [2.0, -1.0]),
           "cell:Q7": entry("Ångström", [0.0, 0.0])},
    "cell_decl_tauceti": {SPECTRUM: entry("Spectrum.compact", [1.5, -2.25]),
                          SEMIGROUP: entry("StronglyContinuousSemigroup.mk", [4.0, 4.5])},
}
LABELS = [
    {"id": "cell:Q1", "label": "Compact operator", "f": 3,
     "aka": ["Compact linear operator", "compact", "Compact", "compact operator"],
     "p": "path:Mathlib/Analysis"},
    {"id": SEMIGROUP, "label": "StronglyContinuousSemigroup.mk", "p": "path:TauCeti/Analysis/Semigroup"},
    {"id": SPECTRUM, "label": "Spectrum.compact", "f": 1, "p": "path:TauCeti/Analysis"},
    # in three folders under two libraries; p names only the first
    {"id": "cell:Q5", "label": "Cauchy–Schwarz inequality", "aka": ["\U0001d53dield inner product"],
     "p": "path:Mathlib/Analysis"},
    {"id": "cell:Q7", "label": "Ångström"},
    {"id": "cell:Q8", "label": "Unplaced thing"},
]
ALIASES = {"_meta": {"counts": {"organs": 6, "decls": 1, "slugs": 1}},
           "organs": {"Q1": "cell:Q1", "decl:TauCeti:Spectrum.compact": SPECTRUM, "Q5": "cell:Q5",
                      "Q7": "cell:Q7", "Q82571": "path:Mathlib/LinearAlgebra", "Q1b": "cell:Q1"},
           "decls": {"Spectrum.compact": SPECTRUM},
           "slugs": {"compact-operator": "cell:Q1"}}
SUPERCELLS = {"_meta": {}, "roots": ["frontier:Physics", "path:Mathlib", "path:TauCeti"],
              "supercells": {
                  "path:Mathlib": {"label": "Mathlib", "fa": 3,
                                   "children": ["path:Mathlib/Analysis", "path:Mathlib/LinearAlgebra"]},
                  "path:Mathlib/Analysis": {"label": "Analysis", "fa": 3, "parent": "path:Mathlib",
                                            "cells": ["cell:Q1", "cell:Q5"]},
                  "path:Mathlib/LinearAlgebra": {
                      "label": "LinearAlgebra", "parent": "path:Mathlib", "cells": ["cell:Q5"],
                      "organs": [{"kind": "concept", "id": "Q82571", "label": "Linear algebra"}],
                      "syn": [{"id": "cell:Q1", "w": 1, "kinds": {"links": 1}}], "counts": {"syn": 1}},
                  "path:TauCeti": {"label": "TauCeti", "fa": 1, "children": ["path:TauCeti/Analysis"]},
                  "path:TauCeti/Analysis": {"label": "Analysis", "fa": 1, "parent": "path:TauCeti",
                                            "children": ["path:TauCeti/Analysis/Semigroup"],
                                            "cells": ["cell:Q5", SPECTRUM]},
                  "path:TauCeti/Analysis/Semigroup": {"label": "Semigroup",
                                                      "parent": "path:TauCeti/Analysis",
                                                      "cells": [SEMIGROUP]},
                  "frontier:Physics": {"label": "Physics", "frontier": True, "cells": ["cell:Q7"],
                                       "stateability": 0.5},
              }}
TRACES = {
    "cell_q": {"cell:Q1|path:Mathlib/LinearAlgebra": {"tt": 1, "traces": [
        {"kind": "links", "src": "Q1", "dst": "Q82571", "prov": 0, "evidence": {"confidence": 0.4}}]}},
    "path_": {"path:Mathlib/Analysis|path:TauCeti/Analysis": {"tt": 30, "traces": []}},
}
XREF = {"xref:nlab:compact": ["Q1", "decl:TauCeti:Spectrum.compact"], "xref:mw:Ring": ["Q1"]}
NODES = [
    {"_meta": {"schema": "brain/SCHEMA.md"}},
    {"id": "Q82571", "label": "Linear algebra", "description": "branch of mathematics"},
    {"id": "decl:TauCeti:Spectrum.compact", "label": "Spectrum.compact",
     "docstring": "The spectrum of a compact operator is countable."},
    {"id": "Q404", "label": "Orphan", "description": "no atom owns this node"},
    # a second organ of cell:Q1, a line before Q1 itself: text_ids keep line order in an owner
    {"id": "Q1b", "label": "Compact map", "description": "an older name"},
    {"id": "Q1", "label": "Compact operator", "description": "operator mapping bounded sets to relatively compact sets"},
    {"id": "path:Mathlib", "label": "Mathlib", "description": "the Lean mathematical library"},
    {"id": "Q2", "label": "No text here"},
]


def shards_for(labels):
    """SHARDS plus an entry for every label that has none, in one extra shard."""
    known = {cid for shard in SHARDS.values() for cid in shard}
    extra = {r["id"]: entry(r["label"], [0.0, 0.0]) for r in labels if r["id"] not in known}
    return {**SHARDS, "extra": extra} if extra else SHARDS


def under_today(labels, supercells, prefix):
    """brain-api.ts filterFor's `under` test for cells (1443-1476), ported line by line."""
    def in_prefix(p):
        return p == prefix or p.startswith(prefix + "/")
    under_set = set()
    for path, e in supercells.items():
        if not in_prefix(path):
            continue
        for cid in e.get("cells") or []:
            under_set.add(cid)
    return [i for i, r in enumerate(labels) if in_prefix(r.get("p") or "") or r["id"] in under_set]


class ServingDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cells = self.tmp / "cells"
        self.nodes = self.tmp / "nodes.jsonl"
        self.write_tree()

    def write_tree(self, *, shards=SHARDS, labels=LABELS, supercells=SUPERCELLS, nodes=NODES,
                   generated_at="2026-11-07T00:00:00Z", trace_meta=None, traces_rows=TRACES,
                   xref=XREF):
        self.cells.mkdir(exist_ok=True)
        (self.cells / "traces").mkdir(exist_ok=True)
        traces = {"dir": "traces", "files": {key: len(rows) for key, rows in traces_rows.items()},
                  **(trace_meta or {})}
        manifest = {"_meta": {"generated_at": generated_at},
                    "shards": {key: len(rows) for key, rows in shards.items()}, "traces": traces}
        for name, doc in (("manifest.json", manifest), ("labels.json", labels),
                          ("aliases.json", ALIASES), ("supercells.json", supercells),
                          ("explorer.json", {"nodes": []})):
            (self.cells / name).write_text(json.dumps(doc))
        for key, rows in shards.items():
            (self.cells / f"{key}.json").write_text(json.dumps(rows))
        for key, rows in traces_rows.items():
            (self.cells / "traces" / f"{key}.json").write_text(json.dumps(rows))
        (self.tmp / "xref_index.json").write_text(json.dumps(xref))
        self.nodes.write_text("".join(json.dumps(n) + "\n" for n in nodes))

    def build(self, name="serving.sqlite3"):
        out = self.tmp / name
        sidecar = bsd.build(self.cells, self.nodes, out,
                            sidecar_path=self.tmp / f"{name}.tables.json")
        return sidecar, sqlite3.connect(out)

    def test_cell_pages_keep_shard_bytes(self):
        _, db = self.build()
        rows = dict(db.execute("SELECT id, json FROM cell_pages"))
        expected = {cid: e for shard in SHARDS.values() for cid, e in shard.items()}
        self.assertEqual(sorted(rows), sorted(expected))
        for cid, e in expected.items():
            self.assertEqual(rows[cid], json.dumps(e, ensure_ascii=False, separators=(",", ":")))
        self.assertTrue(rows["cell:Q1"].startswith('{"syn":[{"w":2,'))

    def test_cell_shards_must_agree_with_each_other_and_the_manifest(self):
        twice = {**SHARDS, "zz": {"cell:Q5": entry("Cauchy–Schwarz again", [9.0, 9.0])}}
        self.write_tree(shards=twice)
        with self.assertRaisesRegex(ValueError, "cell:Q5 is in two cell shards"):
            self.build()
        self.write_tree()
        manifest = json.loads((self.cells / "manifest.json").read_text())
        manifest["shards"]["ce"] += 1
        (self.cells / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "the manifest says 5"):
            self.build()
        self.write_tree(labels=LABELS[:-1])
        with self.assertRaisesRegex(ValueError, "1 shard entries have no label"):
            self.build()
        self.assertFalse((self.tmp / "serving.sqlite3").exists())

    def test_aliases_and_labels(self):
        _, db = self.build()
        self.assertEqual(db.execute("SELECT owner FROM aliases WHERE kind = 'decl' AND key = 'Spectrum.compact'").fetchone(),
                         (SPECTRUM,))
        self.assertEqual(db.execute("SELECT count(*) FROM aliases").fetchone(), (8,))
        self.assertEqual([r[0] for r in db.execute("SELECT id FROM labels ORDER BY ord")], [r["id"] for r in LABELS])
        self.assertEqual(db.execute("SELECT aka, f, p, libs, xy FROM labels WHERE ord = 0").fetchone(),
                         ("Compact linear operator\x1fcompact\x1fCompact\x1fcompact operator", 3,
                          "path:Mathlib/Analysis", "Mathlib", "[0.5,1.0]"))
        libs = dict(db.execute("SELECT id, libs FROM labels"))
        self.assertEqual(libs["cell:Q5"], "Mathlib\x1fTauCeti")
        self.assertEqual(libs[SEMIGROUP], "TauCeti")
        self.assertIsNone(libs["cell:Q7"])       # a frontier area is not a library
        self.assertIsNone(libs["cell:Q8"])

    def test_supercell_fields(self):
        _, db = self.build()
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT path, ord, parent, n_cells, n_sub, fhist, kids, syn, aka, frontier, json, label, fa "
            "FROM supercells")}
        self.assertEqual(sorted(rows, key=lambda p: rows[p][0]), list(SUPERCELLS["supercells"]))
        for path, r in rows.items():
            src = SUPERCELLS["supercells"][path]
            self.assertEqual((r[1], r[10], r[11]), (src.get("parent"), src.get("label"), src.get("fa")), path)
        self.assertEqual(rows["path:Mathlib/Analysis"][1], "path:Mathlib")
        self.assertIsNone(rows["path:Mathlib"][1])
        n_sub = {p: r[3] for p, r in rows.items()}
        # Q5 sits in two Mathlib folders and counts once for path:Mathlib
        self.assertEqual(n_sub, {"path:Mathlib": 2, "path:Mathlib/Analysis": 2, "path:Mathlib/LinearAlgebra": 1,
                                 "path:TauCeti": 3, "path:TauCeti/Analysis": 3,
                                 "path:TauCeti/Analysis/Semigroup": 1, "frontier:Physics": 1})
        self.assertEqual(json.loads(rows["path:Mathlib"][4]),
                         {"3|Mathlib": 1, "0|Mathlib\x1fTauCeti": 1})
        self.assertEqual(json.loads(rows["frontier:Physics"][4]), {"0|": 1})
        for path, r in rows.items():
            self.assertEqual(sum(json.loads(r[4]).values()), r[3], path)
        self.assertEqual(json.loads(rows["path:Mathlib"][5]), [
            {"path": "path:Mathlib/Analysis", "n_sub": 2, "fhist": {"3|Mathlib": 1, "0|Mathlib\x1fTauCeti": 1}},
            {"path": "path:Mathlib/LinearAlgebra", "n_sub": 1, "fhist": {"0|Mathlib\x1fTauCeti": 1}}])
        self.assertIsNone(rows["path:Mathlib/Analysis"][5])
        self.assertEqual(rows["path:Mathlib/LinearAlgebra"][2], 1)
        self.assertIsNone(rows["path:Mathlib"][2])
        self.assertEqual(json.loads(rows["path:Mathlib/LinearAlgebra"][6]),
                         SUPERCELLS["supercells"]["path:Mathlib/LinearAlgebra"]["syn"])
        self.assertEqual(rows["path:Mathlib/LinearAlgebra"][7], "Linear algebra")
        self.assertEqual(rows["frontier:Physics"][8], 1)
        self.assertEqual(json.loads(rows["path:TauCeti/Analysis"][9]),
                         SUPERCELLS["supercells"]["path:TauCeti/Analysis"])

    def test_supercell_aka_reads_labels_like_the_worker(self):
        # `label ?? id` per organ: an empty label stays empty, only a missing one gives the id
        sc = json.loads(json.dumps(SUPERCELLS))
        sc["supercells"]["path:Mathlib"]["organs"] = [{"kind": "concept", "id": "Q9", "label": ""},
                                                      {"kind": "concept", "id": "Q10"}]
        self.write_tree(supercells=sc)
        _, db = self.build()
        self.assertEqual(db.execute("SELECT aka FROM supercells WHERE path = 'path:Mathlib'").fetchone(),
                         ("\x1fQ10",))

    def test_kids_keep_the_64_largest_children(self):
        sc = json.loads(json.dumps(SUPERCELLS))
        big = sc["supercells"]
        kids = [f"path:Big/K{i:02}" for i in range(70)]
        rows = {"path:Big": {"label": "Big", "children": kids}}
        labels = list(LABELS)
        for i, kid in enumerate(kids):
            cells = [f"cell:B{i}x{j}" for j in range(i % 3)]
            rows[kid] = {"label": kid, "parent": "path:Big", "cells": cells}
            labels += [{"id": c, "label": c} for c in cells]
        sc["supercells"] = {**{p: big[p] for p in big if p.startswith("path:")}, **rows,
                            "frontier:Physics": big["frontier:Physics"]}
        sc["supercells"] = dict(sorted(sc["supercells"].items(), key=lambda kv: (kv[0].startswith("frontier:"), kv[0])))
        self.write_tree(shards=shards_for(labels), labels=labels, supercells=sc)
        _, db = self.build()
        got = json.loads(db.execute("SELECT kids FROM supercells WHERE path = 'path:Big'").fetchone()[0])
        self.assertEqual(len(got), bsd.KIDS_CAP)
        want = sorted(kids, key=lambda k: (-(int(k[-2:]) % 3), k))[:bsd.KIDS_CAP]
        self.assertEqual([k["path"] for k in got], want)

    def test_name_keys_lowercase_dedupe_and_len16(self):
        sidecar, db = self.build()
        rows = db.execute("SELECT seq, name_lc, kind, owner, len16, name FROM name_keys ORDER BY seq").fetchall()
        q1 = [(r[1], r[2], r[5]) for r in rows if r[3] == "cell:Q1"]
        # "Compact" folds onto "compact" (the first spelling stays); the label and the aka
        # "compact operator" are different kinds and both stay
        self.assertEqual(q1, [("compact operator", "label", "Compact operator"),
                              ("compact linear operator", "aka", "Compact linear operator"),
                              ("compact", "aka", "compact"),
                              ("compact operator", "aka", "compact operator")])
        for _, name_lc, _, _, len16, name in rows:
            self.assertEqual(name_lc, name.lower())
            self.assertEqual(len16, len(name_lc.encode("utf-16-le")) // 2)
        astral = [r for r in rows if r[5].startswith("\U0001d53d")]
        self.assertEqual([(r[1], r[4]) for r in astral], [("\U0001d53dield inner product", 20)])
        self.assertEqual(db.execute("SELECT name_lc FROM name_keys WHERE owner = 'cell:Q7'").fetchone(), ("ångström",))
        # cells in labels order, then supercells in file order, which start at sup_names_from
        owners = [r[3] for r in rows]
        cell_owners = [o for o in owners if o.startswith("cell:")]
        self.assertEqual(list(dict.fromkeys(cell_owners)), [r["id"] for r in LABELS])
        self.assertEqual(sidecar["sup_names_from"], len(cell_owners) + 1)
        self.assertTrue(all(o.startswith(("path:", "frontier:")) for o in owners[len(cell_owners):]))
        self.assertEqual(sidecar["longest_name"], len("StronglyContinuousSemigroup.mk"))
        sup = {(r[2], r[1]) for r in rows if r[3] == "path:Mathlib/LinearAlgebra"}
        self.assertEqual(sup, {("slabel", "linearalgebra"), ("spath", "mathlib/linearalgebra"),
                               ("sorgan", "linear algebra")})
        # spath is the page's area-search hay (build_brain_page.py:4524)
        self.assertIn(("spath", "physics"), {(r[2], r[1]) for r in rows if r[3] == "frontier:Physics"})

    def test_longest_name_counts_cell_names_only(self):
        sc = json.loads(json.dumps(SUPERCELLS))
        long_path = "path:TauCeti/Analysis/Semigroup/VeryLongModuleNameBeyondThirtyUnits"
        sc["supercells"][long_path] = {"label": "VeryLongModuleNameBeyondThirtyUnits",
                                       "parent": "path:TauCeti/Analysis/Semigroup"}
        sc["supercells"] = dict(sorted(sc["supercells"].items(), key=lambda kv: (kv[0].startswith("frontier:"), kv[0])))
        self.write_tree(supercells=sc)
        sidecar, db = self.build()
        self.assertGreater(db.execute("SELECT max(len16) FROM name_keys").fetchone()[0], 30)
        self.assertEqual(sidecar["longest_name"], len("StronglyContinuousSemigroup.mk"))

    def test_hot_prefixes_are_complete(self):
        labels = list(LABELS) + [{"id": f"cell:T{i}", "label": f"TauCeti.Thm{i:04}"} for i in range(1100)]
        labels += [{"id": f"cell:U{i}", "label": f"Tau{i}"} for i in range(30)]
        # 199 code points but 200 UTF-16 units, the longest query; one unit more is past it
        astral = "a" * 198 + "\U0001d53d"
        labels += [{"id": f"cell:A{i}", "label": f"{astral}x{i:04}"} for i in range(1001)]
        sc = json.loads(json.dumps(SUPERCELLS))
        # empty folders under a hot prefix: the agent list keeps them, the page's drops them
        for i in range(120):
            sc["supercells"][f"path:TauCeti/M{i:03}"] = {"label": f"M{i:03}", "parent": "path:TauCeti"}
        sc["supercells"] = dict(sorted(sc["supercells"].items(), key=lambda kv: (kv[0].startswith("frontier:"), kv[0])))
        self.write_tree(shards=shards_for(labels), labels=labels, supercells=sc)
        sidecar, db = self.build()
        keys = db.execute("SELECT seq, name_lc, owner FROM name_keys ORDER BY seq").fetchall()
        n_sub = dict(db.execute("SELECT path, n_sub FROM supercells"))
        names = sorted(k[1] for k in keys)

        def starting(prefix):
            return bisect.bisect_left(names, prefix + "\U0010ffff") - bisect.bisect_left(names, prefix)

        prefixes = {name[:n] for name in names for n in range(1, len(name) + 1)}
        expected = {p for p in prefixes if 2 <= len(p.encode("utf-16-le")) // 2 <= 200 and starting(p) > bsd.HOT_PREFIX_NAMES}
        got = {r[0]: (json.loads(r[1]), json.loads(r[2]), json.loads(r[3]))
               for r in db.execute("SELECT prefix, cells, sups, live_sups FROM hot_prefixes ORDER BY seq")}
        self.assertIn("tauceti.thm", expected)
        self.assertIn(astral, expected)
        self.assertNotIn(astral + "x", expected)
        self.assertEqual(set(got), expected)
        self.assertEqual(list(got), sorted(expected))
        for prefix, lists in got.items():
            for which, listed in enumerate(lists):
                first, want = set(), []
                for seq, name_lc, owner in keys:
                    is_sup = seq >= sidecar["sup_names_from"]
                    if is_sup == (which > 0) and (which < 2 or n_sub[owner] > 0) \
                            and name_lc.startswith(prefix) \
                            and owner not in first and len(want) < bsd.HOT_PREFIX_OWNERS:
                        first.add(owner)
                        want.append(seq)
                self.assertEqual(listed, want, (prefix, which))
        self.assertEqual(len(got["tau"][0]), 100)
        self.assertEqual(len(got["tau"][1]), 100)
        # TauCeti, TauCeti/Analysis and TauCeti/Analysis/Semigroup hold cells; the M folders don't
        self.assertEqual([keys[seq - 1][2] for seq in got["tau"][2]],
                         ["path:TauCeti", "path:TauCeti/Analysis", "path:TauCeti/Analysis/Semigroup"])

    def test_cell_under_matches_todays_under_test(self):
        # a label whose p names a folder that does not list it: cell_under takes both signals
        labels = LABELS + [{"id": "cell:Q9", "label": "Drifted", "p": "path:Mathlib/LinearAlgebra"}]
        self.write_tree(shards=shards_for(labels), labels=labels)
        _, db = self.build()
        paths = list(SUPERCELLS["supercells"]) + [r["p"] for r in labels if r.get("p")]
        candidates = {p for path in paths for p in bsd.slash_prefixes(path)}
        candidates |= {"path:Mathlib/Anal", "path:Mathlib/", "path:", "path:TauCeti/Analysis/Semi",
                       "frontier:Phys", "nowhere"}
        for prefix in sorted(candidates):
            got = [r[0] for r in db.execute("SELECT ord FROM cell_under WHERE anc = ? ORDER BY ord", (prefix,))]
            self.assertEqual(got, under_today(labels, SUPERCELLS["supercells"], prefix), prefix)
        self.assertIn(6, [r[0] for r in db.execute("SELECT ord FROM cell_under WHERE anc = 'path:Mathlib/LinearAlgebra'")])
        # the multi-folder cell: p names Mathlib/Analysis, membership adds the rest
        self.assertIn(3, [r[0] for r in db.execute("SELECT ord FROM cell_under WHERE anc = 'path:TauCeti'")])
        anc = db.execute("SELECT anc FROM cell_under ORDER BY seq").fetchall()
        self.assertEqual(anc, sorted(anc))

    def test_traces_xrefs_and_text_ids(self):
        _, db = self.build()
        traces = dict(db.execute("SELECT pair, json FROM synapse_traces ORDER BY seq"))
        self.assertEqual({p: json.loads(v) for p, v in traces.items()},
                         {p: v for bucket in TRACES.values() for p, v in bucket.items()})
        self.assertEqual(list(traces), sorted(traces))
        self.assertEqual(db.execute("SELECT page_ord, node_ord, page, node FROM xrefs ORDER BY seq").fetchall(),
                         [(0, 0, "xref:nlab:compact", "Q1"), (0, 1, "xref:nlab:compact", "decl:TauCeti:Spectrum.compact"),
                          (1, 0, "xref:mw:Ring", "Q1")])
        rows = db.execute("SELECT node, owner, body FROM text_ids ORDER BY seq").fetchall()
        # owners' order: cells in labels order, then supercells in file order; Q404 has no owner
        self.assertEqual(rows, [
            ("Q1b", "cell:Q1", "Compact map an older name"),
            ("Q1", "cell:Q1", "Compact operator operator mapping bounded sets to relatively compact sets"),
            ("decl:TauCeti:Spectrum.compact", SPECTRUM, "Spectrum.compact The spectrum of a compact operator is countable."),
            ("path:Mathlib", "path:Mathlib", "Mathlib the Lean mathematical library"),
            ("Q82571", "path:Mathlib/LinearAlgebra", "Linear algebra branch of mathematics")])

    def test_xrefs_list_a_node_once_per_page(self):
        # a community xref repeating a base one lists the node twice; it keeps its first place
        self.write_tree(xref={"xref:nlab:compact": ["Q1", "Q7", "Q1", "Q5", "Q7"], "xref:mw:Ring": ["Q1"]})
        _, db = self.build()
        self.assertEqual(db.execute("SELECT page_ord, node_ord, node FROM xrefs ORDER BY seq").fetchall(),
                         [(0, 0, "Q1"), (0, 1, "Q7"), (0, 2, "Q5"), (1, 0, "Q1")])

    def test_a_trace_pair_in_two_buckets_fails_the_build(self):
        pair = "cell:Q1|path:Mathlib/LinearAlgebra"
        self.write_tree(traces_rows={**TRACES, "zz": {pair: {"tt": 2, "traces": []}}})
        with self.assertRaises(sqlite3.IntegrityError):
            self.build()
        self.assertFalse((self.tmp / "serving.sqlite3").exists())
        self.assertFalse((self.tmp / "serving.sqlite3.tables.json").exists())

    def test_nodes_must_start_with_meta(self):
        self.write_tree(nodes=NODES[1:])
        with self.assertRaisesRegex(ValueError, "_meta"):
            self.build()

    def test_fts_indexes_are_keyed_by_seq(self):
        _, db = self.build()
        hits = db.execute("SELECT k.owner FROM names JOIN name_keys k ON k.seq = names.rowid "
                          "WHERE names MATCH ? ORDER BY k.seq", ('"Semigroup"',)).fetchall()
        self.assertEqual(hits, [(SEMIGROUP,), ("path:TauCeti/Analysis/Semigroup",),
                                ("path:TauCeti/Analysis/Semigroup",)])
        texts = db.execute("SELECT t.node FROM texts JOIN text_ids t ON t.seq = texts.rowid "
                           "WHERE texts MATCH ? ORDER BY bm25(texts)", ("spectrum countable",)).fetchall()
        self.assertEqual(texts, [("decl:TauCeti:Spectrum.compact",)])
        # contentless: the indexes hold no copy of the text, which lives in name_keys and text_ids
        self.assertEqual(db.execute("SELECT name FROM names WHERE rowid = 1").fetchone(), (None,))
        self.assertEqual(db.execute("SELECT body FROM texts WHERE rowid = 1").fetchone(), (None,))

    def test_refuses_an_old_sqlite(self):
        # trigram arrived in 3.34; the replay image's 3.40.1 must stay above the floor
        self.assertLessEqual(bsd.MIN_SQLITE, (3, 40, 1))
        with mock.patch.object(bsd.sqlite3, "sqlite_version_info", (3, 33, 0)), \
                self.assertRaisesRegex(RuntimeError, "trigram tokenizer needs 3.34.0 or newer"):
            self.build()

    def test_every_table_has_seq_and_a_digest_over_every_column(self):
        with mock.patch.object(bsd, "DELETE_BOUND_BYTES", 300):
            sidecar, db = self.build()
        self.assertEqual(set(sidecar), {"schema", "schema_version", "tables", "longest_name", "sup_names_from",
                                        "aliases_counts"})
        self.assertEqual(sidecar["schema_version"], bsd.SCHEMA_VERSION)
        kinds = {"organs": "organ", "decls": "decl", "slugs": "slug"}
        self.assertEqual(sidecar["aliases_counts"], ALIASES["_meta"]["counts"])
        self.assertEqual(sidecar["aliases_counts"],
                         {field: db.execute("SELECT count(*) FROM aliases WHERE kind = ?", (kind,)).fetchone()[0]
                          for field, kind in kinds.items()})
        self.assertEqual(dict(db.execute("SELECT key, value FROM meta"))["unicode_version"],
                         unicodedata.unidata_version)
        # seq follows each table's key order (name_keys and text_ids are checked on their own)
        key_order = {"cell_pages": ("id",), "aliases": ("kind", "key"), "labels": ("ord",),
                     "supercells": ("ord",), "cell_under": ("anc", "ord"), "synapse_traces": ("pair",),
                     "xrefs": ("page_ord", "node_ord"), "hot_prefixes": ("prefix",)}
        self.assertEqual(set(sidecar["tables"]), {"cell_pages", "aliases", "labels", "supercells", "name_keys",
                                                  "hot_prefixes", "cell_under", "synapse_traces", "xrefs", "text_ids"})
        recorded = {n: {"rows": r, "digest": d, "delete_bounds": json.loads(b)}
                    for n, r, d, b in db.execute("SELECT name, rows, digest, delete_bounds FROM tables")}
        self.assertEqual(recorded, sidecar["tables"])
        for table, meta in sidecar["tables"].items():
            cursor = db.execute(f"SELECT * FROM {table} ORDER BY seq")
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, r)) for r in cursor]
            self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)), table)
            self.assertEqual(meta["rows"], len(rows), table)
            self.assertEqual(meta["digest"], digest_rows(rows), table)
            if table in key_order:
                keyed = [tuple(r[k] for k in key_order[table]) for r in rows]
                self.assertEqual(keyed, sorted(keyed), table)
            bounds, total, mark = [], 0, 300
            for r in rows:
                total += len(canonical_json(r).encode()) + 1
                if total >= mark:
                    bounds.append(r["seq"])
                    mark = (total // 300 + 1) * 300
            self.assertEqual(meta["delete_bounds"], bounds, table)
        self.assertIn("name", [c[1] for c in db.execute("PRAGMA table_info(name_keys)")])
        self.assertIn("body", [c[1] for c in db.execute("PRAGMA table_info(text_ids)")])
        self.assertTrue(sidecar["tables"]["cell_pages"]["delete_bounds"])
        self.assertTrue(sidecar["tables"]["aliases"]["rows"] > 1 and sidecar["tables"]["cell_pages"]["rows"] > 1)

    def test_sidecar_is_identical_across_rebuilds_and_binds_fts_inputs(self):
        self.build("a.sqlite3")
        # the generation stamp is not part of the dataset's identity
        self.write_tree(generated_at="2027-01-01T00:00:00Z")
        self.build("b.sqlite3")
        first = (self.tmp / "a.sqlite3.tables.json").read_bytes()
        self.assertEqual(first, (self.tmp / "b.sqlite3.tables.json").read_bytes())
        self.assertNotIn(b"2026-11-07", first)
        # a changed description changes text_ids' digest, so the dataset id binds FTS input
        nodes = [dict(n) for n in NODES]
        next(n for n in nodes if n.get("id") == "Q1")["description"] = "a different description"
        self.write_tree(nodes=nodes)
        third, _ = self.build("c.sqlite3")
        before = json.loads(first)["tables"]
        self.assertNotEqual(third["tables"]["text_ids"]["digest"], before["text_ids"]["digest"])
        self.assertEqual(third["tables"]["name_keys"], before["name_keys"])

    def test_no_real_column_anywhere(self):
        _, db = self.build()
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                           "AND sql NOT LIKE 'CREATE VIRTUAL%'")]
        self.assertIn("labels", tables)
        for table in tables:
            for _, column, kind, *_ in db.execute(f"PRAGMA table_info({table})"):
                self.assertNotIn(kind.upper(), ("REAL", "FLOAT", "DOUBLE", "NUMERIC"), (table, column))
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE typeof("{column}") = \'real\'')
                                 .fetchone(), (0,), (table, column))
        # a float headed for an INTEGER column fails the build instead of shipping a REAL
        labels = [dict(r) for r in LABELS]
        labels[0]["f"] = 2.5
        self.write_tree(labels=labels)
        with self.assertRaisesRegex(ValueError, "REAL"):
            self.build("float.sqlite3")

    def test_refuses_a_tree_whose_traces_carry_their_own_prov(self):
        self.write_tree(trace_meta={"prov": [{"source": "skewed"}]})
        with self.assertRaisesRegex(ValueError, "prov"):
            self.build()
        self.assertFalse((self.tmp / "serving.sqlite3").exists())

    def test_cell_shards_fail_on_prov_skew(self):
        data = self.tmp / "data"
        make_fixture(data)
        paths = dict(cells_path=data / "cells.jsonl", synapses_path=data / "synapses.jsonl",
                     nodes_path=data / "nodes.jsonl", edges_path=data / "edges.jsonl",
                     frontier_path=data / "frontier.jsonl", frontier_graph_path=data / "frontier_graph.json",
                     scratch_parent=self.tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            build_cell_shards.build_cell_shards(output_dir=self.tmp / "good", **paths)
        # the tree the shards stage writes is what the serving build reads
        (self.tmp / "xref_index.json").write_text("{}")
        sidecar = bsd.build(self.tmp / "good", data / "nodes.jsonl", self.tmp / "real.sqlite3",
                            xref_path=self.tmp / "xref_index.json")
        self.assertEqual(sidecar["tables"]["cell_pages"]["rows"], 2)

        lines = (data / "synapses.jsonl").read_text().splitlines()
        meta = json.loads(lines[0])["_meta"]
        meta["prov"] = list(reversed(PROV))
        write_jsonl(data / "synapses.jsonl", meta, [json.loads(line) for line in lines[1:]])
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(SystemExit, "prov table differs"):
            build_cell_shards.build_cell_shards(output_dir=self.tmp / "skewed", **paths)
        self.assertFalse((self.tmp / "skewed").exists())

    def leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("."))

    def test_publishes_both_files_atomically(self):
        out, side = self.tmp / "serving.sqlite3", self.tmp / "serving-tables.json"
        bsd.build(self.cells, self.nodes, out)
        self.assertTrue(side.exists())
        old = (out.read_bytes(), side.read_bytes())
        self.assertEqual(self.leftovers(), [])

        # a failed build changes neither file
        (self.cells / "labels.json").write_text("[not json")
        with self.assertRaises(ValueError):
            bsd.build(self.cells, self.nodes, out)
        self.assertEqual((out.read_bytes(), side.read_bytes()), old)
        self.assertEqual(self.leftovers(), [])

        # a failed second rename puts the first file back
        labels = [dict(r) for r in LABELS]
        labels[0]["label"] = "Renamed operator"
        self.write_tree(labels=labels)
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst) == side and str(src).endswith(".tmp"):
                raise OSError("disk full")
            return real_replace(src, dst)
        with mock.patch.object(bsd.os, "replace", side_effect=replace), self.assertRaises(OSError):
            bsd.build(self.cells, self.nodes, out)
        self.assertEqual((out.read_bytes(), side.read_bytes()), old)
        self.assertEqual(self.leftovers(), [])

        bsd.build(self.cells, self.nodes, out)
        self.assertNotEqual(out.read_bytes(), old[0])
        self.assertNotEqual(side.read_bytes(), old[1])

    def test_a_failed_restore_keeps_its_backup(self):
        out, side = self.tmp / "serving.sqlite3", self.tmp / "serving-tables.json"
        bsd.build(self.cells, self.nodes, out)
        old = (out.read_bytes(), side.read_bytes())
        labels = [dict(r) for r in LABELS]
        labels[0]["label"] = "Renamed operator"
        self.write_tree(labels=labels)
        real_replace = os.replace

        def replace(src, dst):
            # the sidecar's forward rename fails, and so does putting the database back
            if Path(dst) == side or str(src).endswith(".old"):
                raise OSError("disk gone")
            return real_replace(src, dst)
        with mock.patch.object(bsd.os, "replace", side_effect=replace), self.assertRaises(OSError) as caught:
            bsd.build(self.cells, self.nodes, out)
        # the sidecar was never replaced; the database's old bytes survive in its backup link
        self.assertEqual(side.read_bytes(), old[1])
        self.assertNotEqual(out.read_bytes(), old[0])
        notes = " ".join(getattr(caught.exception, "__notes__", []))
        self.assertIn(f"could not restore {out}", notes)
        self.assertNotIn(f"could not restore {side}", notes)
        kept = [name for name in self.leftovers() if name.endswith(".old")]
        self.assertEqual(len(kept), 1)
        self.assertEqual((self.tmp / kept[0]).read_bytes(), old[0])

    def test_outputs_are_0644_and_alone(self):
        # the replay runner accepts a stage's outputs only at 0644, with no undeclared file
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        old = os.umask(0o077)
        try:
            bsd.build(self.cells, self.nodes, out_dir / "serving.sqlite3")
        finally:
            os.umask(old)
        self.assertEqual({p.name: p.stat().st_mode & 0o777 for p in out_dir.iterdir()},
                         {"serving.sqlite3": 0o644, "serving-tables.json": 0o644})


if __name__ == "__main__":
    unittest.main()
