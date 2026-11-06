#!/usr/bin/env python3
"""Tests for scripts/pipeline/index_from_corpus.py, on a synthetic corpus database.

The fixture is built here, in SQLite, with the ``cf-corpus/1`` schema and nothing of a
real one: four declarations over two libraries, one of them tagged ``@[wikidata]``, one
at a commit the universe does not pin, one Prop-valued definition. No network, no Lean,
and no corpus file on the host is read.

What is held:

* the converted index loads through :mod:`index` and answers at the universe's commit;
* the seven :data:`index.ROW_KEYS` come across unchanged — the converter decides none
  of them — and ``statement`` comes back a bool;
* the ``@[wikidata]`` tag is the one :mod:`candidates` makes a curated hit from;
* a row at a commit the universe does not pin is left out, and counted;
* a corpus of another universe, another schema, or a missing column is **refused**;
* the search corpus is the same declarations in a shape Grep reads.

Run: python3 scripts/pipeline/test_index_from_corpus.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import candidates as candidates_mod  # noqa: E402
import index as index_mod  # noqa: E402
import index_from_corpus as convert_mod  # noqa: E402

UNIVERSE_SHA = "a" * 64
MATHLIB = "3dfb8e22b670" + "0" * 28
OTHER = "bf3266149cda" + "0" * 28
TESTLIB = "4453b52d2468" + "0" * 28

#: (id, library, commit, name, module, path, line, end_line, kind, proof_status,
#:  statement, signature, docstring)
DECLS = [
    (1, "mathlib", MATHLIB, "MonoidHom", "Mathlib.Algebra.Group.Hom.Defs",
     "Mathlib/Algebra/Group/Hom/Defs.lean", 10, 24, "structure", "definition", 0,
     "(M : Type u) → (N : Type v) → [MulOneClass M] → [MulOneClass N] → Type (max u v)",
     "Bundled monoid homomorphisms."),
    (2, "mathlib", MATHLIB, "map_mul", "Mathlib.Algebra.Group.Hom.Defs",
     "Mathlib/Algebra/Group/Hom/Defs.lean", 99, None, "theorem", "proved", 0,
     "∀ {M N : Type} [inst : Mul M] (f : M →* N) (a b : M), f (a * b) = f a * f b",
     None),
    (3, "mathlib", OTHER, "map_mul", "Mathlib.Algebra.Group.Hom.Defs",
     "Mathlib/Algebra/Group/Hom/Defs.lean", 98, None, "theorem", "proved", 0,
     "a signature at another commit", None),
    (4, "testlib", TESTLIB, "Widget.IsRound", "Testlib.Widget",
     "Testlib/Widget.lean", 3, 5, "def", "definition", 1,
     "(w : Widget) → Prop", "Whether a widget is round."),
]
#: (decl id, tag, value)
TAGS = [(1, "wikidata", "Q868169"), (1, "wikipedia",
                                     "https://en.wikipedia.org/wiki/Monoid_homomorphism"),
        (2, "wikidata", "not-a-qid")]


def build_corpus(path: Path, *, schema: str = "cf-corpus/1",
                 universe_sha: str = UNIVERSE_SHA, decls=DECLS,
                 columns: str | None = None) -> Path:
    """Write a synthetic ``cf-corpus/1`` database and return its path."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE libraries (id TEXT PRIMARY KEY, repo TEXT NOT NULL,
            commit_sha TEXT NOT NULL, declarations INTEGER NOT NULL);
        CREATE TABLE tags (decl_id INTEGER NOT NULL, tag TEXT NOT NULL,
            value TEXT NOT NULL);
    """)
    conn.execute(f"CREATE TABLE decls ({columns or '''
        id INTEGER PRIMARY KEY, library TEXT NOT NULL, commit_sha TEXT NOT NULL,
        name TEXT NOT NULL, module TEXT, path TEXT, line INTEGER, end_line INTEGER,
        kind TEXT, proof_status TEXT NOT NULL, statement INTEGER NOT NULL DEFAULT 0,
        signature TEXT, docstring TEXT'''})")
    conn.executemany("INSERT INTO meta VALUES (?, ?)",
                     [("schema", schema), ("universe", "u-test"),
                      ("universe_sha256", universe_sha), ("built", "2026-01-01T00:00:00Z")])
    conn.executemany("INSERT INTO libraries VALUES (?, ?, ?, ?)",
                     [("mathlib", "https://example.invalid/mathlib", MATHLIB, 2),
                      ("testlib", "https://example.invalid/testlib", TESTLIB, 1)])
    if columns is None:
        conn.executemany("INSERT INTO decls VALUES (" + ", ".join("?" * 13) + ")", decls)
        conn.executemany("INSERT INTO tags VALUES (?, ?, ?)", TAGS)
    conn.commit()
    conn.close()
    return path


class Converted(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.corpus = build_corpus(self.dir / "corpus.sqlite")
        self.out = self.dir / "decl-index.jsonl"
        self.search = self.dir / "search"
        self.provenance = convert_mod.convert(corpus=self.corpus, out=self.out,
                                              search=self.search)

    # -- the index ------------------------------------------------------------------

    def test_the_index_loads_and_is_pinned_to_the_corpus_universe(self):
        idx = index_mod.open_index(self.out, universe_sha256=UNIVERSE_SHA)
        self.assertEqual(idx.universe_sha256, UNIVERSE_SHA)
        self.assertEqual(len(idx), 3)       # the fourth row is at another commit

    def test_a_declaration_answers_at_the_commit_the_universe_pins(self):
        idx = index_mod.open_index(self.out)
        row = idx.declaration("mathlib", "map_mul", MATHLIB)
        self.assertEqual(row["proof_status"], "proved")
        self.assertEqual(row["kind"], "theorem")
        self.assertEqual(row["module"], "Mathlib.Algebra.Group.Hom.Defs")
        self.assertEqual(row["line"], 99)
        self.assertIsNone(row["end_line"])
        self.assertIs(row["statement"], False)
        self.assertIsNone(idx.declaration("mathlib", "map_mul", OTHER))
        self.assertIsNone(idx.declaration("mathlib", "NoSuchDeclaration", MATHLIB))

    def test_every_row_key_is_carried_and_none_is_decided_here(self):
        rows = [json.loads(line) for line in
                self.out.read_text(encoding="utf-8").splitlines()[1:]]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(set(index_mod.ROW_KEYS) - set(row), set())
            self.assertEqual({"library", "commit", "declaration"} - set(row), set())
        prop = next(r for r in rows if r["declaration"] == "Widget.IsRound")
        # the Prop-valued-definition flag is the corpus's, read across as a bool
        self.assertIs(prop["statement"], True)
        self.assertEqual(prop["commit"], TESTLIB)

    def test_a_row_at_another_commit_than_the_universe_pins_is_left_out_and_counted(self):
        self.assertEqual(
            self.provenance["rows_at_another_commit_than_the_universe_pins"], 1)
        self.assertEqual(self.provenance["rows"], 3)

    # -- the tag ---------------------------------------------------------------------

    def test_the_wikidata_tag_is_the_join_the_retrieval_makes_a_curated_hit_from(self):
        retrieval = candidates_mod.Retrieval.load(self.out)
        hits = candidates_mod.curated_hits("Q868169", retrieval)
        self.assertEqual(hits, [{"library": "mathlib", "declaration": "MonoidHom",
                                 "via": candidates_mod.VIA_WIKIDATA_TAG,
                                 "evidence": "@[wikidata Q868169]"}])
        self.assertEqual(candidates_mod.curated_hits("Q999999999", retrieval), [])

    def test_a_tag_value_that_is_no_qid_is_not_a_tag(self):
        self.assertEqual(self.provenance["rows_with_wikidata_tag"], 1)
        rows = {json.loads(line)["declaration"]: json.loads(line) for line in
                self.out.read_text(encoding="utf-8").splitlines()[1:]}
        self.assertNotIn("wikidata", rows["map_mul"])

    def test_the_catalog_join_is_off_unless_it_is_asked_for(self):
        catalog = self.dir / "catalog"
        catalog.mkdir()
        (catalog / "pilot_tagged.jsonl").write_text(
            json.dumps({"primary_decl": "map_mul", "wikidata_qid": "Q123"}) + "\n",
            encoding="utf-8")
        plain = convert_mod.convert(corpus=self.corpus, out=self.dir / "a.jsonl")
        self.assertEqual(plain["rows_tagged_from_catalog"], 0)
        joined = convert_mod.convert(corpus=self.corpus, out=self.dir / "b.jsonl",
                                     catalog=catalog)
        self.assertEqual(joined["rows_tagged_from_catalog"], 1)
        # the corpus's own tag wins where it has one
        self.assertEqual(joined["tags_by_source"]["corpus @[wikidata]"], 1)
        retrieval = candidates_mod.Retrieval.load(self.dir / "b.jsonl")
        self.assertEqual([hit["declaration"] for hit
                          in candidates_mod.curated_hits("Q123", retrieval)], ["map_mul"])

    # -- the refusals -----------------------------------------------------------------

    def test_a_corpus_built_for_another_universe_is_refused(self):
        universe = self.dir / "universe.json"
        universe.write_text(json.dumps({"id": "u-other", "universe_sha256": "b" * 64}),
                            encoding="utf-8")
        with self.assertRaises(convert_mod.CorpusIndexProblem) as caught:
            convert_mod.convert(corpus=self.corpus, out=self.dir / "x.jsonl",
                                universe=universe)
        self.assertIn("universe", str(caught.exception))

    def test_a_corpus_of_an_unknown_schema_is_refused(self):
        other = build_corpus(self.dir / "other.sqlite", schema="something-else/9")
        with self.assertRaises(convert_mod.CorpusIndexProblem):
            convert_mod.convert(corpus=other, out=self.dir / "x.jsonl")

    def test_a_decls_table_missing_a_row_key_is_refused_by_name(self):
        thin = build_corpus(self.dir / "thin.sqlite",
                            columns="id INTEGER PRIMARY KEY, library TEXT, "
                                    "commit_sha TEXT, name TEXT, proof_status TEXT")
        with self.assertRaises(convert_mod.CorpusIndexProblem) as caught:
            convert_mod.convert(corpus=thin, out=self.dir / "x.jsonl")
        self.assertIn("end_line", str(caught.exception))

    def test_a_universe_yaml_is_read_for_its_two_scalars(self):
        yaml = self.dir / "universe.yaml"
        yaml.write_text("# a comment\nid: u-test\nfrozen: \"2026-01-01\"\n"
                        f"universe_sha256: {UNIVERSE_SHA}\nlibraries:\n  - id: mathlib\n",
                        encoding="utf-8")
        self.assertEqual(convert_mod.read_universe(yaml), ("u-test", UNIVERSE_SHA))
        got = convert_mod.convert(corpus=self.corpus, out=self.dir / "y.jsonl",
                                  universe=yaml)
        self.assertEqual(got["universe_sha256"], UNIVERSE_SHA)

    # -- the search corpus --------------------------------------------------------------

    def test_the_search_corpus_is_the_same_declarations_in_a_shape_grep_reads(self):
        for name in convert_mod.SEARCH_FILES:
            self.assertTrue((self.search / name).is_file(), name)
        lines = (self.search / "declarations.tsv").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(len(line.split("\t")), 6)
        self.assertIn("mathlib\tMonoidHom\tstructure\tdefinition", lines[0])
        signatures = (self.search / "signatures.tsv").read_text(encoding="utf-8")
        self.assertIn("f (a * b) = f a * f b", signatures)
        self.assertNotIn("at another commit", signatures)
        self.assertEqual((self.search / "wikidata.tsv").read_text(encoding="utf-8"),
                         "mathlib\tMonoidHom\tQ868169\n")

    def test_a_signature_holding_a_newline_stays_one_line(self):
        decls = [(1, "mathlib", MATHLIB, "A", "M", "M.lean", 1, None, "theorem", "proved",
                  0, "line one\nline\ttwo", "doc\nstring")]
        corpus = build_corpus(self.dir / "wrapped.sqlite", decls=decls)
        convert_mod.convert(corpus=corpus, out=self.dir / "w.jsonl",
                            search=self.dir / "wsearch")
        for name in ("signatures.tsv", "docstrings.tsv"):
            text = (self.dir / "wsearch" / name).read_text(encoding="utf-8")
            self.assertEqual(len(text.splitlines()), 1, name)
            self.assertEqual(len(text.splitlines()[0].split("\t")), 3, name)

    # -- the command line ---------------------------------------------------------------

    def test_the_command_line_writes_the_index_and_its_provenance(self):
        out = self.dir / "cli.jsonl"
        prov = self.dir / "cli-provenance.json"
        proc = subprocess.run(
            [sys.executable, str(HERE / "index_from_corpus.py"),
             "--corpus", str(self.corpus), "--out", str(out), "--provenance", str(prov)],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("3 declaration(s)", proc.stdout)
        self.assertEqual(json.loads(prov.read_text())["rows"], 3)

    def test_a_corpus_that_is_not_there_exits_two_and_prints_nothing(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "index_from_corpus.py"),
             "--corpus", str(self.dir / "nope.sqlite"), "--out", str(self.dir / "o.jsonl")],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
