#!/usr/bin/env python3
"""build_serving_db turns a cell-asset tree into the serving database the D1 import reads."""
from __future__ import annotations

import bisect
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_cell_shards  # noqa: E402
import build_serving_db as bsd  # noqa: E402
from store import canonical_json, digest_rows, read_metadata  # noqa: E402
from test_store import PROV, make_fixture, write_jsonl  # noqa: E402

SPECTRUM = "cell:decl:TauCeti:Spectrum.compact"
SEMIGROUP = "cell:decl:TauCeti:StronglyContinuousSemigroup.mk"


def entry(label, xy, syn=(), sups=None):
    return {"cell": {"label": label, "xy": xy, **({"supercells": sups} if sups else {})},
            "organs": [], "syn": list(syn)}


SHARDS = {
    # neither the shard's ids nor cell:Q1's keys are in sorted order, so a builder that
    # sorts keys, or numbers rows in file order, shows
    "ce": {"cell:Q8": entry("Unplaced thing", [3.0, 3.0]),
           "cell:Q1": {"syn": [{"w": 2, "id": SPECTRUM}], "cell": {"xy": [0.5, 1.0], "label": "Compact operator"},
                       "organs": []},
           "cell:Q5": entry("Cauchy–Schwarz inequality", [2.0, -1.0]),
           "cell:Q7": entry("Ångström", [0.0, 0.0])},
    # lone TauCeti decls: the layout puts each in its module's bubble
    "cell_decl_tauceti": {SPECTRUM: entry("Spectrum.compact", [1.5, -2.25], sups=["path:TauCeti/Analysis"]),
                          SEMIGROUP: entry("StronglyContinuousSemigroup.mk", [4.0, 4.5],
                                           sups=["path:TauCeti/Analysis/Semigroup"])},
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
# the discs the layout drew for those two cells' modules (cells.jsonl _meta.bubbles)
BUBBLES = {"path:TauCeti/Analysis": [120.0, -40.5, 50.0],
           "path:TauCeti/Analysis/Semigroup": [180.0, 60.0, 50.0]}
# explorer.json's edges by id; SPECTRUM - SEMIGROUP crosses two TauCeti modules, so SPECTRUM
# sits in the ripple of path:TauCeti/Analysis/Semigroup; Q8 has no p and no edge
EDGES = [("cell:Q1", SPECTRUM, 2), (SPECTRUM, SEMIGROUP, 3), ("cell:Q1", "cell:Q5", 1),
         ("cell:Q5", "cell:Q7", 1)]
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


def explorer_doc(labels, shards, edges=EDGES):
    """explorer.json as build_cell_shards writes it for these labels and shards (1182-1211)."""
    xy = {cid: e["cell"].get("xy") for shard in shards.values() for cid, e in shard.items()}
    order = sorted(labels, key=lambda r: r["id"])
    index = {r["id"]: i for i, r in enumerate(order)}
    nodes = [{"id": r["id"], "label": r["label"], "xy": xy.get(r["id"]),
              **({"f": r["f"]} if r.get("f") else {}), **({"p": r["p"]} if r.get("p") else {})}
             for r in order]
    rows = sorted(([index[a], index[b], w] for a, b, w in edges if a in index and b in index),
                  key=lambda e: (-e[2], e[0], e[1]))
    return {"_meta": {"schema": "brain/SCHEMA.md#v3", "truncated": False,
                      "counts": {"nodes": len(nodes), "edges": len(rows)}},
            "nodes": nodes, "edges": rows}


def static_explorer(doc, focus):
    """build_brain_page.py renderExplorer's scope, keep and edge loops (5298-5340) with no
    filter on, ported line by line: the drawn node ids, the edges as (id, id, w), universeN."""
    nodes, edges = doc["nodes"], doc["edges"]
    total = len(nodes)
    scope = focus if focus.startswith("path:") else None

    def in_sub(pp):
        return pp == scope or (pp or "").startswith(scope + "/")
    keep = [0] * total
    universe = total
    if not scope:
        keep = [1] * total
    else:
        core = [1 if nodes[i].get("p") and in_sub(nodes[i]["p"]) else 0 for i in range(total)]
        touch = list(core)
        for i, k2, _ in edges:
            if core[i]:
                touch[k2] = 1
            if core[k2]:
                touch[i] = 1
        universe = sum(touch)
        keep = touch
    return ([nodes[i]["id"] for i in range(total) if keep[i]],
            [(nodes[i]["id"], nodes[k2]["id"], w) for i, k2, w in edges if keep[i] and keep[k2]],
            universe)


def load_graph(cells_dir, cells_path):
    """The ExplorerGraph a build of this tree draws its scopes from."""
    manifest = json.loads((cells_dir / "manifest.json").read_text())
    xy_by_id, bubbled = {}, {}
    for _ in bsd.cell_page_rows(cells_dir, manifest, xy_by_id, bubbled):
        pass
    labels = json.loads((cells_dir / "labels.json").read_text())
    libs = bsd.subtree(labels, json.loads((cells_dir / "supercells.json").read_text()))[0]
    return bsd.read_explorer(cells_dir, labels, libs, xy_by_id, bubbled,
                             read_metadata(cells_path).get("bubbles", {}))


def stored_scopes(db):
    """{scope: [part json, ...]} from explorer_scopes, checking the parts run 0.. in seq order."""
    parts = {}
    for scope, part, text in db.execute("SELECT scope, part, json FROM explorer_scopes ORDER BY seq"):
        parts.setdefault(scope, []).append((part, text))
    for scope, rows in parts.items():
        assert [p for p, _ in rows] == list(range(len(rows))), scope
    return {scope: [t for _, t in rows] for scope, rows in parts.items()}


def payload_edges(payload):
    """A payload's edges as (id or dot path, id or dot path, w)."""
    names = [n["id"] for n in payload["nodes"]] + [b["path"] for b in payload["bubbles"]]
    return [(names[i], names[j], w) for i, j, w in payload["edges"]]


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


def five_module_graph():
    """A graph whose scope path:T has a ripple of two modules, path:U/R (2 members) and path:U/Q
    (1), and three core modules, path:T/Z (5), path:T/A (2) and path:T/M (2), plus one plain core
    cell that every ripple cell bonds with. Long labels make every collapse shrink the payload."""
    homes = (["path:T/Z"] * 5 + ["path:T/A"] * 2 + ["path:T/M"] * 2 + [None]
             + ["path:U/R"] * 2 + ["path:U/Q"])
    ids = [f"cell:{i:02}" for i in range(len(homes))]
    p = [home or "path:T" for home in homes]
    nodes = [bsd.explorer_node(c, "x" * 200, [0.0, 0.0], 0, q) for c, q in zip(ids, p)]
    edges = [(9, j, 1) for j in (10, 11, 12)]
    return bsd.ExplorerGraph(ids, nodes, p, [0] * len(ids), [None] * len(ids), edges, homes,
                             {m: [1.0, 2.0, 3.0] for m in homes if m})


class ServingDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cells = self.tmp / "cells"
        self.nodes = self.tmp / "nodes.jsonl"
        self.write_tree()

    def write_tree(self, *, shards=SHARDS, labels=LABELS, supercells=SUPERCELLS, nodes=NODES,
                   generated_at="2026-11-07T00:00:00Z", trace_meta=None, traces_rows=TRACES,
                   xref=XREF, edges=EDGES, explorer=None, bubbles=BUBBLES, cells_meta=None):
        self.cells.mkdir(exist_ok=True)
        (self.cells / "traces").mkdir(exist_ok=True)
        traces = {"dir": "traces", "files": {key: len(rows) for key, rows in traces_rows.items()},
                  **(trace_meta or {})}
        n_cells = sum(len(rows) for rows in shards.values())
        manifest = {"_meta": {"generated_at": generated_at, "counts": {"cells": n_cells}},
                    "shards": {key: len(rows) for key, rows in shards.items()}, "traces": traces}
        for name, doc in (("manifest.json", manifest), ("labels.json", labels),
                          ("aliases.json", ALIASES), ("supercells.json", supercells),
                          ("explorer.json", explorer or explorer_doc(labels, shards, edges))):
            (self.cells / name).write_text(json.dumps(doc))
        # cells.jsonl beside nodes.jsonl, where the builder looks for it; its _meta is all it reads
        meta = cells_meta or {"schema": "brain/SCHEMA.md#v3", "generated_at": generated_at,
                              "counts": {"cells": n_cells}, **({"bubbles": bubbles} if bubbles else {})}
        (self.tmp / "cells.jsonl").write_text(json.dumps({"_meta": meta}) + "\n")
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
                     "xrefs": ("page_ord", "node_ord"), "hot_prefixes": ("prefix",),
                     "explorer_edges": ("a", "b"), "explorer_scopes": ("scope", "part")}
        self.assertEqual(set(sidecar["tables"]), {"cell_pages", "aliases", "labels", "supercells", "name_keys",
                                                  "hot_prefixes", "cell_under", "synapse_traces", "xrefs", "text_ids",
                                                  "explorer_edges", "explorer_scopes"})
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


    # ---- the explorer tables -------------------------------------------------------------

    def build_db(self, name="serving.sqlite3"):
        """Build, and return the database with its built graph and its stored scopes."""
        _, db = self.build(name)
        return db, load_graph(self.cells, self.tmp / "cells.jsonl"), stored_scopes(db)

    def write_module_tree(self, **kwargs):
        """The fixture plus three more members of the path:TauCeti/Analysis module and two of
        path:TauCeti/Analysis/Semigroup. SEMIGROUP bonds with A1 too, so the Semigroup scope's
        ripple holds two Analysis members (SPECTRUM and A1) and its core three cells."""
        extra = [{"id": f"cell:decl:TauCeti:Spectrum.a{i}", "label": f"Spectrum.a{i}", "f": 2,
                  "p": "path:TauCeti/Analysis"} for i in (1, 2, 3)]
        extra += [{"id": f"cell:decl:TauCeti:StronglyContinuousSemigroup.s{i}", "label": f"Semigroup.s{i}",
                   "p": "path:TauCeti/Analysis/Semigroup"} for i in (1, 2)]
        shards = {**SHARDS, "more": {r["id"]: entry(r["label"], [10.0 + i, 5.0], sups=[r["p"]])
                                     for i, r in enumerate(extra)}}
        sc = json.loads(json.dumps(SUPERCELLS))
        for r in extra:
            sc["supercells"][r["p"]]["cells"].append(r["id"])
        edges = EDGES + [(SEMIGROUP, "cell:decl:TauCeti:Spectrum.a1", 1)]
        self.write_tree(shards=shards, labels=LABELS + extra, supercells=sc, edges=edges, **kwargs)
        return explorer_doc(LABELS + extra, shards, edges)

    def test_the_membership_rule_is_the_layout_s_and_needs_no_numpy(self):
        rule = bsd.bubble_module
        self.assertIsNone(rule("cell:decl:Mathlib:Finset.sum_comm", ["path:Mathlib/Algebra"], 1))
        self.assertIsNone(rule("cell:Q1", ["path:TauCeti/Analysis"], 1))
        self.assertIsNone(rule(SPECTRUM, ["path:TauCeti/Analysis"], 2))      # not lone: simulated
        self.assertEqual(rule(SPECTRUM, ["path:TauCeti/Analysis"], 1), "path:TauCeti/Analysis")
        self.assertEqual(rule(SPECTRUM, None, 0), "path:TauCeti")
        self.assertEqual(rule(SPECTRUM, ["path:TauCeti/B", "path:TauCeti/A/Deep", "path:TauCeti/A/Dee"], 1),
                         "path:TauCeti/A/Dee")                                # the deepest, then path order
        self.assertEqual(rule("cell:decl:Fake:odd:name", [], 1), "path:Fake")
        # requirements-ci.txt does not pin numpy: the rule imports without it, the layout refuses
        code = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); "
                "sys.modules['numpy'] = None; import layout; "
                "print(layout.bubble_module('cell:decl:TauCeti:x', None, 1))\n"
                "try:\n    layout.layout_cells({}, [])\nexcept ModuleNotFoundError as exc:\n    print(exc)")
        done = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent,
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["path:TauCeti", "the layout needs numpy"])

    def test_explorer_edges_keep_every_edge_as_ids(self):
        _, db = self.build()
        rows = db.execute("SELECT seq, a, b, w FROM explorer_edges ORDER BY seq").fetchall()
        self.assertEqual([r[1:] for r in rows], sorted(EDGES, key=lambda e: (e[0], e[1])))
        self.assertEqual([r[0] for r in rows], list(range(1, len(EDGES) + 1)))
        self.assertIn(("index", "explorer_edges_b"),
                      db.execute("SELECT type, name FROM sqlite_master WHERE tbl_name = 'explorer_edges'").fetchall())

    def test_page_drift_guard(self):
        # the transcription below is of these lines; if the page changes them, port it again
        page = (Path(__file__).resolve().parent.parent / "site" / "build_brain_page.py").read_text()
        for line in ('const scope = isPathId(focusId) ? focusId : null;',
                     'const inSub = pp => pp === scope || (pp || "").startsWith(scope + "/");',
                     'if (!scope) {',
                     'for (let i = 0; i < totalN; i++) keep[i] = visRow(i) ? 1 : 0;',
                     'for (let i = 0; i < totalN; i++) if (nodes[i].p && inSub(nodes[i].p)) core[i] = 1;',
                     'for (const [i, k2] of j.edges) {   // one ripple: homeless atoms hanging off the core',
                     'if (core[i]) touch[k2] = 1;',
                     'if (core[k2]) touch[i] = 1;',
                     'if (touch[i]) universeN++;',
                     'keep[i] = (touch[i] && visRow(i)) ? 1 : 0;',
                     'if (!keep[i]) continue;',
                     'for (const [i, k2, w] of j.edges) {',
                     'if (!keep[i] || !keep[k2]) continue;'):
            self.assertIn(line, page)

    def test_every_scope_draws_as_the_static_explorer(self):
        with mock.patch.object(bsd, "LIVE_TOUCH", -1):       # store every scope
            db, graph, stored = self.build_db()
        doc = json.loads((self.cells / "explorer.json").read_text())
        scopes = ["all"] + [p for p in SUPERCELLS["supercells"] if p.startswith("path:")]
        self.assertEqual(sorted(stored), sorted(scopes))
        served = bsd.ExplorerGraph.from_serving(db)
        for scope in scopes:
            payload = bsd.join_parts(stored[scope])
            ids, edges, universe = static_explorer(doc, scope)
            self.assertEqual([n["id"] for n in payload["nodes"]], ids, scope)
            self.assertEqual(payload_edges(payload), edges, scope)
            self.assertEqual(payload["_meta"]["counts"]["universe"], universe, scope)
            self.assertEqual(payload["bubbles"], [], scope)       # nothing too big: no dots
            self.assertEqual(payload["_meta"]["collapsed"], 0, scope)
            # the nodes are explorer.json's, plus their libs
            by_id = {n["id"]: n for n in doc["nodes"]}
            for node in payload["nodes"]:
                bare = {k: v for k, v in node.items() if k != "l"}
                self.assertEqual(bare, by_id[node["id"]])
            # the oracle the Worker's live computation is checked against reads only D1 rows
            self.assertEqual(bsd.explorer_payload(served, scope), payload, scope)
            self.assertEqual(bsd.explorer_payload(graph, scope), payload, scope)
        everything = bsd.join_parts(stored["all"])
        libs = {n["id"]: everything["libs"][n["l"]] for n in everything["nodes"] if "l" in n}
        self.assertEqual(libs["cell:Q5"], "Mathlib\x1fTauCeti")
        self.assertNotIn("cell:Q7", libs)
        # the homeless, edgeless cell is drawn in `all` only
        self.assertIn("cell:Q8", [n["id"] for n in everything["nodes"]])
        # the cross-module edge: the Semigroup scope's ripple holds SPECTRUM, drawn as a cell
        semigroup = bsd.join_parts(stored["path:TauCeti/Analysis/Semigroup"])
        self.assertEqual([n["id"] for n in semigroup["nodes"]], [SPECTRUM, SEMIGROUP])

    def test_live_scopes_get_no_rows_and_the_rest_do(self):
        doc = explorer_doc(LABELS, SHARDS)
        deg = {n["id"]: 0 for n in doc["nodes"]}
        for i, j, _ in doc["edges"]:
            deg[doc["nodes"][i]["id"]] += 1
            deg[doc["nodes"][j]["id"]] += 1
        sizes = {}
        for scope in ["all"] + [p for p in SUPERCELLS["supercells"] if p.startswith("path:")]:
            ids, _, universe = static_explorer(doc, scope)
            sizes[scope] = (universe, sum(deg[c] for c in ids))
        for touch, scan in ((bsd.LIVE_TOUCH, bsd.LIVE_SCAN), (2, bsd.LIVE_SCAN), (100, 5)):
            with self.subTest(touch=touch, scan=scan), mock.patch.object(bsd, "LIVE_TOUCH", touch), \
                    mock.patch.object(bsd, "LIVE_SCAN", scan):
                _, _, stored = self.build_db(f"live-{touch}-{scan}.sqlite3")
            want = {s for s, (u, d) in sizes.items() if s == "all" or u > touch or d > scan}
            self.assertEqual(set(stored), want)
            self.assertIn("all", stored)
        self.assertEqual(sizes["path:Mathlib"], (4, 7))
        self.assertEqual(sizes["path:TauCeti/Analysis/Semigroup"], (2, 3))

    def test_parts_split_at_part_bytes_and_join_back(self):
        with mock.patch.object(bsd, "PART_BYTES", 400), mock.patch.object(bsd, "SCOPE_PARTS", 50):
            _, graph, stored = self.build_db()
            whole = bsd.explorer_payload(graph, "all")
        texts = stored["all"]
        self.assertGreater(len(texts), 1)
        for i, text in enumerate(texts):
            self.assertLessEqual(len(text.encode()), 400)
            part = json.loads(text)
            self.assertEqual(set(part), {"_meta", "libs", "nodes", "bubbles", "edges"} if i == 0
                             else {"nodes", "bubbles", "edges"})
        self.assertEqual(json.loads(texts[0])["_meta"]["parts"], len(texts))
        # the oracle is unpacked: one part whatever PART_BYTES is, and otherwise the same payload
        joined = bsd.join_parts(texts)
        self.assertEqual(whole["_meta"]["parts"], 1)
        self.assertEqual({**joined, "_meta": {**joined["_meta"], "parts": 1}}, whole)
        # dots spill past part 0 like nodes and edges; only _meta and libs must fit in it
        meta = {"scope": "path:X", "parts": 0, "counts": {}, "collapsed": 0}
        dots = [bsd.compact({"path": f"path:X/M{i:02}", "disc": [1.5, 2.5, 3.0], "n": 1}) for i in range(20)]
        with mock.patch.object(bsd, "PART_BYTES", 300):
            texts, head = bsd.pack_parts(dict(meta), ["Lib"], [], dots, ["[0,1,1]"])
            self.assertTrue(any(json.loads(t)["bubbles"] for t in texts[1:]))
            self.assertTrue(all(len(t.encode()) <= 300 for t in texts))
            joined = bsd.join_parts(texts)
            self.assertEqual([b["path"] for b in joined["bubbles"]], [json.loads(d)["path"] for d in dots])
            self.assertEqual(joined["edges"], [[0, 1, 1]])
            self.assertEqual(head, len(f'"_meta":{bsd.compact(joined["_meta"])},"libs":["Lib"]'.encode()))
            self.assertEqual(joined["_meta"]["parts"], len(texts))
            with self.assertRaisesRegex(ValueError, "_meta and libs alone"):
                bsd.pack_parts(dict(meta), ["L" * 400], [], [], [])

    def test_a_scope_too_big_collapses_ripple_modules_first(self):
        doc = self.write_module_tree()
        graph = load_graph(self.cells, self.tmp / "cells.jsonl")
        scope = graph.scope("path:TauCeti/Analysis/Semigroup")
        self.assertEqual(graph.collapse_order(scope), ["path:TauCeti/Analysis", "path:TauCeti/Analysis/Semigroup"])
        a1 = "cell:decl:TauCeti:Spectrum.a1"
        s1, s2 = (f"cell:decl:TauCeti:StronglyContinuousSemigroup.s{i}" for i in (1, 2))
        self.assertEqual(static_explorer(doc, scope.name)[0], [a1, SPECTRUM, SEMIGROUP, s1, s2])
        with mock.patch.object(bsd, "DRAW_CAP", 4):          # 5 cells: the ripple module goes
            texts, stats = bsd.stored_parts(graph, scope)
        payload = bsd.join_parts(texts)
        self.assertEqual(stats["modules"], 1)
        self.assertEqual([n["id"] for n in payload["nodes"]], [SEMIGROUP, s1, s2])
        self.assertEqual(payload["bubbles"], [{"path": "path:TauCeti/Analysis", "disc": BUBBLES["path:TauCeti/Analysis"],
                                               "n": 2, "fa": 3, "l": payload["libs"].index("TauCeti")}])
        self.assertEqual(payload["edges"], [[0, 3, 4]])      # SPECTRUM's 3 and A1's 1, summed
        self.assertEqual(payload["_meta"]["collapsed"], 2)
        with mock.patch.object(bsd, "DRAW_CAP", 3):          # then the core module
            texts, stats = bsd.stored_parts(graph, scope)
        payload = bsd.join_parts(texts)
        self.assertEqual(stats["modules"], 2)
        self.assertEqual(payload["nodes"], [])
        self.assertEqual([(b["path"], b["n"]) for b in payload["bubbles"]],
                         [("path:TauCeti/Analysis", 2), ("path:TauCeti/Analysis/Semigroup", 3)])
        self.assertEqual(payload["edges"], [[0, 1, 4]])
        with mock.patch.object(bsd, "DRAW_CAP", 1), self.assertRaisesRegex(
                ValueError, "path:TauCeti/Analysis/Semigroup does not fit with all 2 of its modules"):
            bsd.stored_parts(graph, scope)

    def test_parts_alone_can_force_a_collapse(self):
        self.write_module_tree()
        graph = load_graph(self.cells, self.tmp / "cells.jsonl")
        scope = graph.scope("path:TauCeti/Analysis/Semigroup")
        order = graph.collapse_order(scope)
        size = [len(bsd.pack_parts(*graph.draw(scope, order[:k]))[0][0].encode()) for k in (0, 1)]
        self.assertGreater(size[0], size[1])
        with mock.patch.object(bsd, "PART_BYTES", size[1]), mock.patch.object(bsd, "SCOPE_PARTS", 1):
            texts, stats = bsd.stored_parts(graph, scope)
        self.assertEqual((stats["modules"], len(texts)), (1, 1))
        self.assertLessEqual(len(texts[0].encode()), size[1])

    def test_the_collapse_order_and_the_search_for_k(self):
        graph = five_module_graph()
        scope = graph.scope("path:T")
        order = graph.collapse_order(scope)
        # ripple modules first, then core ones; each by members in the scope, most first, then path
        self.assertEqual(order, ["path:U/R", "path:U/Q", "path:T/Z", "path:T/A", "path:T/M"])
        self.assertEqual(graph.collapse_order(graph.scope("all")),       # no ripple in `all`
                         ["path:T/Z", "path:T/A", "path:T/M", "path:U/R", "path:U/Q"])

        def search():
            with mock.patch.object(graph, "draw", wraps=graph.draw) as draw:
                texts, stats = bsd.stored_parts(graph, scope)
            return texts, stats, [len(c.args[1]) for c in draw.call_args_list]

        def drawn(k):
            payload = bsd.explorer_payload(graph, "path:T", k)
            return len(payload["nodes"]) + len(payload["bubbles"])
        self.assertEqual([drawn(k) for k in range(6)], [13, 12, 12, 8, 7, 6])
        # the node cap alone: no k below 3 can meet 8 nodes, so the search draws none of them
        with mock.patch.object(bsd, "DRAW_CAP", 8):
            texts, stats, tried = search()
        self.assertEqual((stats["modules"], tried), (3, [3, 5]))     # 5: the full-collapse record
        self.assertEqual(stats["nodes"], 8)
        # bytes alone: doubling tries 0, 1, 2 and 4, then bisecting lands on 3 or stays on 4
        sizes = [len(bsd.pack_parts(*graph.draw(scope, order[:k]))[0][0].encode()) for k in range(6)]
        self.assertEqual(sizes, sorted(set(sizes), reverse=True))    # every collapse shrinks it
        for k in (3, 4):
            with self.subTest(k=k), mock.patch.object(bsd, "PART_BYTES", sizes[k]), \
                    mock.patch.object(bsd, "SCOPE_PARTS", 1):
                texts, stats, tried = search()
                self.assertEqual(tried, [0, 1, 2, 4, 3, 5])
                self.assertEqual((stats["modules"], len(texts)), (k, 1))
                payload = bsd.join_parts(texts)
                self.assertEqual([b["path"] for b in payload["bubbles"]], sorted(order[:k]))
                self.assertEqual(payload, bsd.explorer_payload(graph, "path:T", k))
                self.assertGreater(len(bsd.pack_parts(*graph.draw(scope, order[:k - 1]))[0]), 1)

    def test_a_bubbled_cell_without_a_supercell_joins_its_library_root_module(self):
        # Lone has no supercell, so no p and the module path:TauCeti; Root's one supercell is
        # path:TauCeti, so its p is path:TauCeti: one module, members with and without a p
        lone, root = "cell:decl:TauCeti:Lone", "cell:decl:TauCeti:Root"
        labels = LABELS + [{"id": lone, "label": "Lone"}, {"id": root, "label": "Root", "p": "path:TauCeti"}]
        shards = {**SHARDS, "more": {lone: entry("Lone", [7.0, 7.0]),
                                     root: entry("Root", [8.0, 8.0], sups=["path:TauCeti"])}}
        bubbles = {**BUBBLES, "path:TauCeti": [0.0, 90.0, 50.0]}
        self.write_tree(shards=shards, labels=labels, bubbles=bubbles)
        with mock.patch.object(bsd, "LIVE_TOUCH", -1):
            _, graph, stored = self.build_db()
        module_of = dict(zip(graph.ids, graph.module))
        self.assertEqual((module_of[lone], module_of[root]), ("path:TauCeti", "path:TauCeti"))
        drawn = {name: [n["id"] for n in bsd.join_parts(texts)["nodes"]] for name, texts in stored.items()}
        self.assertEqual([name for name, ids in drawn.items() if lone in ids], ["all"])   # edgeless
        self.assertIn(root, drawn["path:TauCeti"])
        for name, members in (("all", 2), ("path:TauCeti", 1)):
            with self.subTest(scope=name):
                payload = bsd.explorer_payload(graph, name, len(graph.collapse_order(graph.scope(name))))
                dot = next(b for b in payload["bubbles"] if b["path"] == "path:TauCeti")
                self.assertEqual((dot["n"], dot["disc"]), (members, bubbles["path:TauCeti"]))
                self.assertFalse({lone, root} & {n["id"] for n in payload["nodes"]})

    def test_main_reads_cells_jsonl_from_the_cells_flag(self):
        moved = self.tmp / "elsewhere" / "cells.jsonl"
        moved.parent.mkdir()
        (self.tmp / "cells.jsonl").rename(moved)
        # beside nodes.jsonl, where the builder looks without --cells, sits another cell build's
        (self.tmp / "cells.jsonl").write_text(
            json.dumps({"_meta": {**read_metadata(moved), "generated_at": "2026-01-01T00:00:00Z"}}) + "\n")
        out = self.tmp / "cli.sqlite3"
        args = ["--cells-dir", str(self.cells), "--nodes", str(self.nodes), "--out", str(out)]
        with self.assertRaisesRegex(ValueError, "its generated_at differs from the manifest's"):
            bsd.main(args)
        self.assertFalse(out.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bsd.main([*args, "--cells", str(moved)]), 0)
        self.assertTrue(out.exists())

    def test_collapsed_scopes_cover_touch_and_sum_the_static_edges(self):
        doc = self.write_module_tree()
        graph = load_graph(self.cells, self.tmp / "cells.jsonl")
        module_of = dict(zip(graph.ids, graph.module))
        f = {n["id"]: n.get("f") or 0 for n in doc["nodes"]}
        for name in ["all"] + [p for p in SUPERCELLS["supercells"] if p.startswith("path:")]:
            ids, static_edges, universe = static_explorer(doc, name)
            order = graph.collapse_order(graph.scope(name))
            for k in range(len(order) + 1):
                with self.subTest(scope=name, collapse=k):
                    payload = bsd.explorer_payload(graph, name, k)
                    gone = set(order[:k])
                    self.assertEqual([n["id"] for n in payload["nodes"]],
                                     [c for c in ids if module_of[c] not in gone])
                    for dot in payload["bubbles"]:
                        members = [c for c in ids if module_of[c] == dot["path"]]
                        self.assertEqual(dot["n"], len(members))
                        fa = 0
                        for c in members:
                            fa |= f[c]
                        self.assertEqual(dot.get("fa", 0), fa)
                        self.assertEqual(dot["disc"], BUBBLES[dot["path"]])
                    self.assertEqual(len(payload["nodes"]) + sum(d["n"] for d in payload["bubbles"]), universe)

                    def rep(c):
                        return module_of[c] if module_of[c] in gone else c
                    want, got = {}, {}
                    for a, b, w in static_edges:
                        ra, rb = rep(a), rep(b)
                        if ra in gone or rb in gone:
                            if ra != rb:
                                want[frozenset((ra, rb))] = want.get(frozenset((ra, rb)), 0) + w
                        else:
                            want[(ra, rb)] = want.get((ra, rb), 0) + w
                    for a, b, w in payload_edges(payload):
                        key = frozenset((a, b)) if a in gone or b in gone else (a, b)
                        self.assertNotIn(key, got)
                        got[key] = w
                    self.assertEqual(got, want)
                    if gone:
                        keyed = [(-w, i, j) for i, j, w in payload["edges"]]
                        self.assertEqual(keyed, sorted(keyed))

    def test_a_build_collapses_only_what_does_not_fit_and_records_it(self):
        self.write_module_tree()
        with mock.patch.object(bsd, "LIVE_TOUCH", -1), mock.patch.object(bsd, "DRAW_CAP", 6):
            db, graph, stored = self.build_db()
        explorer = json.loads(dict(db.execute("SELECT key, value FROM meta"))["explorer"])
        sizes = explorer["stored_scopes"]
        self.assertEqual(sizes["all"]["modules"], 2)          # 11 cells: 4 + 2 dots
        self.assertEqual(sizes["path:TauCeti"]["modules"], 1)             # 8 cells: 4 + 1 dot
        self.assertEqual(sizes["path:TauCeti/Analysis"]["modules"], 1)
        self.assertEqual(sizes["path:TauCeti/Analysis/Semigroup"]["modules"], 0)   # 5 fit
        self.assertEqual(sizes["path:Mathlib"]["modules"], 0)
        self.assertEqual(explorer["collapsed"], 3)
        # path:TauCeti fits with either core module as a dot; Analysis, the larger, goes first
        self.assertEqual([b["path"] for b in bsd.join_parts(stored["path:TauCeti"])["bubbles"]],
                         ["path:TauCeti/Analysis"])
        self.assertEqual(explorer["caps"]["draw"], 6)
        for scope, texts in stored.items():
            self.assertEqual(sizes[scope]["parts"], len(texts))
            self.assertEqual(sizes[scope]["bytes"], sum(len(t.encode()) for t in texts))
            payload = bsd.join_parts(texts)
            self.assertEqual(sizes[scope]["nodes"], len(payload["nodes"]) + len(payload["bubbles"]))
            self.assertLessEqual(sizes[scope]["nodes"], 6)
            head = len(f'"_meta":{bsd.compact(payload["_meta"])},"libs":{bsd.compact(payload["libs"])}'.encode())
            self.assertEqual(sizes[scope]["header_bytes"], head)
        sc = graph.scope("all")
        full = bsd.pack_parts(*graph.draw(sc, graph.collapse_order(sc)))[0]
        self.assertEqual(sizes["all"]["full_collapse"], {"parts": len(full), "bytes": sum(len(t.encode()) for t in full)})
        self.assertEqual((explorer["scopes"], explorer["live"], explorer["stored"]), (7, 0, 7))
        # nothing fits: the build fails, naming the scope, and publishes nothing
        with mock.patch.object(bsd, "DRAW_CAP", 1), \
                self.assertRaisesRegex(ValueError, "explorer scope all does not fit"):
            self.build("nofit.sqlite3")
        self.assertFalse((self.tmp / "nofit.sqlite3").exists())

    def test_meta_records_the_default_split(self):
        db, graph, stored = self.build_db()
        explorer = json.loads(dict(db.execute("SELECT key, value FROM meta"))["explorer"])
        self.assertEqual(set(stored), {"all"})
        self.assertEqual((explorer["scopes"], explorer["live"], explorer["stored"], explorer["collapsed"]), (7, 6, 1, 0))
        self.assertEqual(explorer["caps"], {"live_touch": 2000, "live_scan": 20000, "draw": 25000,
                                            "part_bytes": 1000000, "scope_parts": 8})
        self.assertNotEqual(explorer["stored_scopes"]["all"]["full_collapse"]["bytes"],
                            explorer["stored_scopes"]["all"]["bytes"])
        inputs = json.loads(dict(db.execute("SELECT key, value FROM meta"))["inputs"])
        self.assertIn("explorer.json", inputs)
        self.assertIn("cells.jsonl", inputs)

    def test_cells_jsonl_must_come_from_the_tree_s_cell_build(self):
        good = {"schema": "brain/SCHEMA.md#v3", "generated_at": "2026-11-07T00:00:00Z",
                "counts": {"cells": 6}, "bubbles": BUBBLES}
        for field, bad in (("generated_at", {**good, "generated_at": "2026-11-08T00:00:00Z"}),
                           ("counts.cells", {**good, "counts": {"cells": 7}}),
                           ("prov", {**good, "prov": [{"source": "other"}]})):
            with self.subTest(field=field):
                self.write_tree(cells_meta=bad)
                with self.assertRaisesRegex(ValueError, f"its {field} differs from the manifest's"):
                    self.build()
                self.assertFalse((self.tmp / "serving.sqlite3").exists())
        # a replay build stamps generation_id too, which the manifest does not copy
        self.write_tree(cells_meta={**good, "generation_id": "2026-11-07T00:00:00Z"})
        self.build()

    def test_bubbles_must_match_the_layout_s_discs(self):
        self.write_tree(bubbles={"path:TauCeti/Analysis": BUBBLES["path:TauCeti/Analysis"]})
        with self.assertRaisesRegex(ValueError, f"{SEMIGROUP}: its module path:TauCeti/Analysis/Semigroup has no disc"):
            self.build()
        self.write_tree(bubbles={**BUBBLES, "path:TauCeti/Empty": [1.0, 2.0, 3.0]})
        with self.assertRaisesRegex(ValueError, "1 discs .* have no member, e.g. path:TauCeti/Empty"):
            self.build()
        self.write_tree(bubbles={**BUBBLES, "path:TauCeti/Analysis": [1.0, 2.0]})
        with self.assertRaisesRegex(ValueError, "is not \\[x, y, r\\]"):
            self.build()
        # a lone decl with no supercell joins path:<Lib>, which must be a folder too
        labels = LABELS + [{"id": "cell:decl:Fake:x", "label": "x"}]
        self.write_tree(shards=shards_for(labels), labels=labels,
                        bubbles={**BUBBLES, "path:Fake": [1.0, 2.0, 3.0]})
        with self.assertRaisesRegex(ValueError, "module path:Fake is not a path: supercell"):
            self.build()
        for sups in (["path:TauCeti", "path:TauCeti/Analysis"], ["path:TauCeti/Analysis/Semigroup"]):
            with self.subTest(sups=sups):
                shards = json.loads(json.dumps(SHARDS))
                shards["cell_decl_tauceti"][SPECTRUM]["cell"]["supercells"] = sups
                self.write_tree(shards=shards)
                with self.assertRaisesRegex(ValueError, f"{SPECTRUM}: a bubbled cell must have one supercell"):
                    self.build()
        self.assertFalse((self.tmp / "serving.sqlite3").exists())

    def test_a_tree_without_bubbles_builds_without_discs(self):
        shards = json.loads(json.dumps(SHARDS))
        for e in shards["cell_decl_tauceti"].values():       # not lone: the layout simulates them
            e["organs"] = [{"kind": "decl"}, {"kind": "statement"}]
        self.write_tree(shards=shards, bubbles=None)
        self.assertNotIn("bubbles", read_metadata(self.tmp / "cells.jsonl"))
        db, graph, stored = self.build_db()
        self.assertEqual(graph.module, [None] * len(LABELS))
        self.assertEqual(graph.collapse_order(graph.scope("all")), [])
        self.assertEqual(bsd.join_parts(stored["all"])["bubbles"], [])

    def test_explorer_json_must_agree_with_the_labels_and_shards(self):
        doc = explorer_doc(LABELS, SHARDS)
        cases = []
        bad = json.loads(json.dumps(doc))
        bad["_meta"]["truncated"] = True
        cases.append((bad, "_meta.truncated is not false"))
        bad = json.loads(json.dumps(doc))
        bad["nodes"][0]["label"] = "Something else"
        cases.append((bad, "disagrees with its label row or shard entry"))
        bad = json.loads(json.dumps(doc))
        bad["nodes"][0]["xy"] = [9.0, 9.0]
        cases.append((bad, "disagrees with its label row or shard entry"))
        bad = json.loads(json.dumps(doc))
        bad["nodes"][0]["p"] = "path:Elsewhere"
        cases.append((bad, "disagrees with its label row or shard entry"))
        bad = json.loads(json.dumps(doc))
        del bad["nodes"][-1]
        cases.append((bad, "lists 5 cells; labels.json has 6"))
        bad = json.loads(json.dumps(doc))
        bad["nodes"].reverse()
        cases.append((bad, "not in id order"))
        bad = json.loads(json.dumps(doc))
        bad["edges"].reverse()
        cases.append((bad, "not in \\(-w, i, j\\) order"))
        bad = json.loads(json.dumps(doc))
        bad["edges"][0][2] = 2.5
        cases.append((bad, "is not \\[node, node, integer weight\\]"))
        bad = json.loads(json.dumps(doc))
        bad["nodes"][0]["extra"] = 1
        cases.append((bad, "fields the serving tables do not carry"))
        bad = json.loads(json.dumps(doc))
        bad["edges"][0][1] = len(bad["nodes"])
        cases.append((bad, "is not \\[node, node, integer weight\\]"))
        bad = json.loads(json.dumps(doc))
        self.assertEqual(bad["nodes"][0]["id"], "cell:Q1")
        bad["nodes"][0]["id"] = "cell:Q0"                    # still in id order, but no label
        cases.append((bad, "node 'cell:Q0' is not a label"))
        for explorer, message in cases:
            with self.subTest(message=message):
                self.write_tree(explorer=explorer)
                with self.assertRaisesRegex(ValueError, message):
                    self.build()
        self.assertFalse((self.tmp / "serving.sqlite3").exists())

    def test_scopes_of_a_large_nested_tree_match_the_page_and_stay_local(self):
        # 3,000 cells over 156 nested folders and about 9,000 edges: every scope comes from
        # p ranges and the adjacency, with no pass over every cell or every edge per scope
        n = 3000
        ids = [f"cell:{i:05}" for i in range(n)]
        p = [f"path:L/{i % 5}/{i % 25}/{i % 125}" if i % 7 else None for i in range(n)]
        pairs = {}
        for i in range(n):
            for k in range(3):
                j = (i * 7919 + k * 104729) % n
                if j != i and (j, i) not in pairs:
                    pairs.setdefault((i, j), 1 + (i + k) % 5)
        edges = sorted(((i, j, w) for (i, j), w in pairs.items()), key=lambda e: (-e[2], e[0], e[1]))
        graph = bsd.ExplorerGraph(ids, [bsd.explorer_node(c, c, [0.0, 0.0], 0, q) for c, q in zip(ids, p)],
                                  p, [0] * n, [None] * n, edges)
        folders = sorted({q for x in p if x for q in bsd.slash_prefixes(x)})
        self.assertEqual(len(folders), 156)

        class Counted:
            """An edge column that counts reads; iterating it would be a pass over every edge."""
            def __init__(self, data):
                self.data, self.reads = data, 0

            def __getitem__(self, i):
                self.reads += 1
                return self.data[i]

            def __len__(self):
                return len(self.data)

            def __iter__(self):
                raise AssertionError("a scope walked every edge")

        class Untouchable:
            """p, which a scope must reach only through its sorted p ranges."""
            def __getitem__(self, i):
                raise AssertionError("a scope read p cell by cell")

            def __iter__(self):
                raise AssertionError("a scope walked every cell's p")
        graph.ea, graph.p = Counted(graph.ea), Untouchable()
        scopes = {}
        for name in folders:
            before = graph.ea.reads
            scopes[name] = graph.scope(name)
            # each edge entry of a touch cell, once: exactly the scan, never the whole edge list
            self.assertLessEqual(graph.ea.reads - before, scopes[name].scan, name)
        self.assertLess(min(sc.scan for sc in scopes.values()), len(edges))
        doc = {"nodes": [{"id": c, **({"p": q} if q else {})} for c, q in zip(ids, p)],
               "edges": [list(e) for e in edges]}
        for name in folders[::6]:                            # 26 of them against the page's loops
            touch, static_edges, universe = static_explorer(doc, name)
            sc = scopes[name]
            self.assertEqual([ids[u] for u in sc.touch], touch, name)
            self.assertEqual(len(sc.touch), universe, name)
            self.assertEqual([(ids[graph.ea[e]], ids[graph.eb[e]], graph.ew[e]) for e in sc.edges],
                             static_edges, name)
            inside = set(touch)
            self.assertEqual(sc.scan, sum((ids[a] in inside) + (ids[b] in inside) for a, b, _ in edges), name)


if __name__ == "__main__":
    unittest.main()
