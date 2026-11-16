#!/usr/bin/env python3
"""Tests for stages 6b and 4b: a sealed statement set, written and read back.

New code. ``wikilean.statement-set/1`` (docs/STATEMENTS.md) publishes a cut on its own,
before any locate run, so the four things held here are the four the two phases rest on:

* **round trip** — the set reads back into exactly the ``{source_id: [passage]}`` the
  writer was handed, so phase 2 plans from a published set as it plans from a harvested
  cut, and the locate task manifests are the same manifests;
* **the seal** — the schema, its eight keys, every file's sha256 and nothing else in the
  directory; a tampered, stray or missing file is refused;
* **key parity with `keys.py`** — every sealed ``passage_key`` is
  ``keys.passage_key(pageid, keys.span_sha256(layer, span))``, which is the key
  :mod:`records` writes on a location record of the same passage in phase 2, so a
  community tag written in phase 1 lands on the pipeline's own row;
* **every refusal** — one test per rule the writer and the reader hold a statement to.

Everything is synthetic: ``scripts/pipeline/fixtures/statementset/``, which this also
rebuilds from ``make_statement_fixture.py`` and refuses a difference, so the committed
fixture cannot drift from the writer. WikiLean is public, so no article text.

Run: python3 scripts/pipeline/test_statement_sets.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE / "gate", HERE / "fixtures", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import export_records  # noqa: E402
import export_statements  # noqa: E402
import keys  # noqa: E402
import make_statement_fixture as fixture  # noqa: E402
import records as records_mod  # noqa: E402
import sourceset as sourceset_mod  # noqa: E402
import statements as statements_mod  # noqa: E402
import vocab  # noqa: E402

FIXTURE = HERE / "fixtures" / "statementset"
LAYERS = {sid: fixture.layer_of(sid)[0] for sid in fixture.CUTS}
PINS = {"set_id": fixture.SET_ID, "campaign": fixture.CAMPAIGN,
        "sourceset": fixture.SOURCESET, "sourceset_manifest": fixture.SOURCESET_MANIFEST,
        "producer_commit": fixture.PRODUCER_COMMIT, "created": fixture.CREATED,
        "span_end_rule": fixture.SPAN_END_RULE,
        "statement_cut_rule": fixture.STATEMENT_CUT_RULE}
FIRST = "enwiki-1005-3006"


def tree(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes()
            for p in sorted(path.rglob("*")) if p.is_file()}


def inventory() -> dict[str, list[dict]]:
    return {sid: fixture.passages(sid) for sid in fixture.CUTS}


def write_set(out: Path, *, inventory_rows=None, sources=None, layers=None, **over) -> dict:
    """The fixture's export, with one thing changed."""
    pins = {**PINS, **over}
    return export_statements.build_set(
        inventory=inventory() if inventory_rows is None else inventory_rows,
        sources=fixture.sources() if sources is None else sources,
        layers=LAYERS if layers is None else layers, out=out, **pins)


def render(root: Path, layers=None) -> Path:
    """A rendered sources directory: each source's layer and the identity layer map."""
    layers = LAYERS if layers is None else layers
    for sid, layer in layers.items():
        directory = root / sid
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "layer.txt").write_text(layer, encoding="utf-8")
        (directory / "layer_map.json").write_text(json.dumps(
            {"schema": "cf-layer-map/2", "renderer": vocab.RENDERER_WEB_HTML,
             "segments": [[0, len(layer), 0, len(layer), 0]]}), encoding="utf-8")
    return root


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


# ---- the committed fixture -------------------------------------------------------------


class FixtureTests(Temp):
    def test_the_committed_fixture_is_what_the_writer_writes(self):
        rebuilt = self.root / "statementset"
        fixture.write(rebuilt)
        self.assertEqual(tree(rebuilt), tree(FIXTURE),
                         "scripts/pipeline/fixtures/statementset/ has drifted from "
                         "make_statement_fixture.py")

    def test_a_re_export_over_the_set_is_byte_identical(self):
        out = self.root / "set"
        write_set(out)
        before = tree(out)
        write_set(out)
        self.assertEqual(tree(out), before)

    def test_the_fixture_covers_every_shape_the_schema_has(self):
        st = statements_mod.read_set(FIXTURE)
        rows = [row for source in st.statements.values() for row in source]
        self.assertEqual({row["scope"] for row in rows}, vocab.SCOPES)
        self.assertEqual({row["finder"] for row in rows}, vocab.FINDERS)
        self.assertEqual({row["prominence"] for row in rows}, vocab.PROMINENCES)
        self.assertEqual({row["shape"] for row in rows if "shape" in row},
                         {vocab.SHAPE_EXISTENCE_UNIQUENESS, vocab.SHAPE_CASES})
        self.assertTrue(any(row["quote"]["suffix"] for row in rows))
        self.assertTrue(any(row["head_words"] > vocab.QUOTE_MAX_WORDS for row in rows))
        self.assertTrue(any(row["fragment"] is None for row in rows))
        self.assertIsNone(st.sources["enwiki-1007-3008"]["wikidata"])

    def test_the_counts_are_the_cuts(self):
        summary = statements_mod.counts(statements_mod.read_set(FIXTURE))
        self.assertEqual(summary["sources"], 2)
        self.assertEqual(summary["statements"],
                         sum(len(rows) for rows in fixture.CUTS.values()))
        self.assertEqual(summary["located"],
                         sum(1 for rows in fixture.CUTS.values() for row in rows
                             if row["scope"] == vocab.SCOPE_FORMAL_MATH))


# ---- the round trip ----------------------------------------------------------------------


class RoundTripTests(Temp):
    def test_the_set_reads_back_into_the_cut_it_was_written_from(self):
        read = statements_mod.frozen_inventory(FIXTURE)
        for sid, rows in inventory().items():
            with self.subTest(source=sid):
                want = [{key: row[key] for key in
                         (*vocab.STATEMENT_KEYS, *vocab.STATEMENT_OPTIONAL) if key in row}
                        for row in rows]
                # the writer recomputes both keys; the harvested cut states neither
                for row, got in zip(want, read[sid]):
                    row["span_sha256"] = got["span_sha256"]
                    row["passage_key"] = got["passage_key"]
                self.assertEqual(read[sid], want)

    def test_every_sealed_statement_carries_the_documented_keys_and_no_more(self):
        for sid, rows in statements_mod.read_set(FIXTURE).statements.items():
            for row in rows:
                with self.subTest(source=sid, item=row["item"]):
                    self.assertEqual(set(row) - set(vocab.STATEMENT_KEYS),
                                     set(vocab.STATEMENT_OPTIONAL) & set(row))
                    self.assertFalse(set(vocab.STATEMENT_KEYS) - set(row))

    def test_the_reading_copy_span_comes_from_the_render_and_is_not_sealed(self):
        sources = render(self.root / "sources")
        read = statements_mod.frozen_inventory(FIXTURE, sources)
        for sid, rows in read.items():
            for row in rows:
                # the synthetic reading copy IS the layer, so its map is the identity
                self.assertEqual(row["text_span"], row["span"])
        sealed = json.loads(
            (FIXTURE / f"statements/{FIRST}.jsonl").read_text(encoding="utf-8").split("\n")[0])
        self.assertNotIn("text_span", sealed)

    def test_a_render_that_is_not_the_pinned_one_is_refused(self):
        changed = {**LAYERS, FIRST: LAYERS[FIRST].replace("sprocket", "cog", 1)}
        sources = render(self.root / "sources", changed)
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.frozen_inventory(FIXTURE, sources)
        self.assertIn("the set was cut from", str(caught.exception))

    def test_a_span_whose_text_moved_under_an_unchanged_layer_sha_is_refused(self):
        """The span check is per statement, not only per layer: a set whose spans do not
        hash to its own values is refused even when the layer is the pinned one."""
        out = self.root / "set"
        write_set(out)
        path = out / f"statements/{FIRST}.jsonl"
        lines = path.read_text(encoding="utf-8").split("\n")
        row = json.loads(lines[0])
        row["span"] = [row["span"][0], row["span"][1] - 1]
        lines[0] = keys.canonical(row)
        path.write_text("\n".join(lines), encoding="utf-8")
        seal = json.loads((out / "SEAL.json").read_text(encoding="utf-8"))
        seal["files"][f"statements/{FIRST}.jsonl"] = keys.sha256_file(path)
        (out / "SEAL.json").write_text(keys.canonical_line(seal), encoding="utf-8")
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.frozen_inventory(out, render(self.root / "sources"))
        self.assertIn("the cut and the render disagree", str(caught.exception))

    def test_a_sourceset_that_moved_on_is_named(self):
        st = statements_mod.read_set(FIXTURE)
        entries = {sid: dict(row) for sid, row in st.sources.items()}
        self.assertEqual(statements_mod.manifest_problems(st, entries), [])
        entries[FIRST]["layer_sha256"] = "0" * 64
        del entries["enwiki-1007-3008"]
        found = statements_mod.manifest_problems(st, entries)
        self.assertEqual(len(found), 2)
        self.assertIn("layer_sha256", found[0])
        self.assertIn("pins no entry", found[1])


# ---- the keys -----------------------------------------------------------------------------


class KeyParityTests(unittest.TestCase):
    def test_every_passage_key_is_the_one_keys_py_gives(self):
        st = statements_mod.read_set(FIXTURE)
        for sid, rows in st.statements.items():
            layer = LAYERS[sid]
            pageid = int(st.sources[sid]["pageid"])
            for row in rows:
                with self.subTest(source=sid, item=row["item"]):
                    span_sha = keys.span_sha256(layer, row["span"])
                    self.assertEqual(row["span_sha256"], span_sha)
                    self.assertEqual(row["passage_key"], keys.passage_key(pageid, span_sha))

    def test_the_key_is_the_one_a_phase_two_record_will_carry(self):
        """The whole point of the two phases: a location record of the same passage, with
        the set's own cut, recomputes the statement set's ``passage_key`` byte for byte."""
        st = statements_mod.read_set(FIXTURE)
        universe = {"id": "u-fixture", "sha256": "22" * 32,
                    "libraries": [{"id": "widgetlib",
                                   "repo": "https://github.com/example-org/widgetlib",
                                   "commit": "a" * 40}]}
        for sid, rows in st.statements.items():
            source = {**st.sources[sid], "uri": f"https://example.invalid/{sid}",
                      "container_title": "Wikipedia", "licence": "CC-BY-SA-4.0"}
            for row in rows:
                if row["scope"] != vocab.SCOPE_FORMAL_MATH:
                    continue
                with self.subTest(source=sid, item=row["item"]):
                    made, _ = records_mod.passage_records(
                        source,
                        {"source_id": sid, "item": row["item"],
                         "passage_kind": row["passage_kind"], "span": row["span"],
                         "section": row["section"], "prominence": row["prominence"],
                         "challenge": vocab.CHALLENGE_NOT_REQUIRED,
                         "outcome": vocab.OUTCOME_UNRESOLVED, "locations": []},
                        records_mod.Cut(dict(row["quote"]), row["span_sha256"]),
                        universe, status_rule=vocab.STATUS_RULE_BROAD)
                    self.assertEqual(
                        made[0]["extensions"][vocab.LOCATE]["passage_key"],
                        row["passage_key"])

    def test_a_statement_that_states_another_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for field, value in (("passage_key", "0" * 64), ("span_sha256", "1" * 64)):
                rows = inventory()
                rows[FIRST][0][field] = value
                with self.subTest(field=field):
                    report = write_set(Path(tmp) / field, inventory_rows=rows)
                    self.assertEqual(len(report["refused"]), 1)
                    self.assertIn(field, report["refused"][0]["reason"])


# ---- the seal -------------------------------------------------------------------------------


class SealTests(Temp):
    def seal(self) -> dict:
        return json.loads((FIXTURE / "SEAL.json").read_text(encoding="utf-8"))

    def test_the_seal_carries_its_eight_keys_and_no_more(self):
        seal = self.seal()
        self.assertEqual(sorted(seal), sorted(vocab.STATEMENT_SEAL_KEYS))
        self.assertEqual(seal["schema"], vocab.STATEMENT_SET_SCHEMA)
        self.assertEqual(sorted(seal["cut_rules"]), sorted(vocab.STATEMENT_CUT_RULE_KEYS))
        self.assertEqual(sorted(seal["files"]),
                         ["sources.jsonl", *(f"statements/{sid}.jsonl"
                                             for sid in sorted(fixture.CUTS))])
        # no universe, no receipts, no status rule: a cut says nothing about Lean
        self.assertNotIn("universe", seal)
        self.assertNotIn("status_rule", seal)
        self.assertNotIn("skill_commit", seal)

    def test_every_file_the_seal_names_hashes_to_what_it_says(self):
        seal = self.seal()
        for rel, sha in seal["files"].items():
            self.assertEqual(keys.sha256_file(FIXTURE / rel), sha, rel)

    def copy(self) -> Path:
        out = self.root / "set"
        shutil.copytree(FIXTURE, out)
        return out

    def test_a_tampered_file_is_refused(self):
        out = self.copy()
        (out / "sources.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(out)
        self.assertIn("sha256 does not match the seal", str(caught.exception))

    def test_a_file_the_seal_does_not_name_is_refused(self):
        out = self.copy()
        (out / "statements" / "extra.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(out)
        self.assertIn("not in the seal", str(caught.exception))

    def test_a_file_the_seal_names_and_the_set_lacks_is_refused(self):
        out = self.copy()
        (out / f"statements/{FIRST}.jsonl").unlink()
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(out)
        self.assertIn("missing", str(caught.exception))

    def test_a_record_set_is_not_a_statement_set(self):
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(HERE / "fixtures" / "recordset")
        self.assertIn(vocab.STATEMENT_SET_SCHEMA, str(caught.exception))

    def test_a_seal_with_a_stray_or_missing_key_is_refused(self):
        for change, expected in (({"universe": {"id": "u"}}, "and no more"),
                                 ({"cut_rules": None}, "cut_rules is"),
                                 ({"cut_rules": {"span_end_rule": 3}}, "cut_rules is"),
                                 ({"cut_rules": {"span_end_rule": 0,
                                                 "statement_cut_rule": 4}}, "1 or more"),
                                 ({"producer_commit": "nope"}, "40 lowercase hex"),
                                 ({"created": "yesterday"}, "ISO 8601"),
                                 ({"campaign": "run:"}, "campaign is"),
                                 ({"set_id": "!"}, "set_id is"),
                                 ({"sourceset": {"id": "ss"}}, "sourceset is"),
                                 ({"sourceset": {"id": "!", "manifest_sha256": "66" * 32}},
                                  "sourceset.id is"),
                                 ({"sourceset": {"id": "ss", "manifest_sha256": "nope"}},
                                  "sourceset.manifest_sha256 is"),
                                 ({"cut_rules": {"span_end_rule": 3,
                                                 "statement_cut_rule": "4"}}, "1 or more"),
                                 ({"files": {}}, "files maps each relative path"),
                                 ({"schema": "cf-locate-recordset/2"},
                                  vocab.STATEMENT_SET_SCHEMA)):
            with self.subTest(change=sorted(change)):
                self.assertTrue(statements_mod._seal_problems({**self.seal(), **change}))
                found = "\n".join(statements_mod._seal_problems({**self.seal(), **change}))
                self.assertIn(expected, found)

    def test_a_seal_missing_a_key_is_refused(self):
        seal = {key: value for key, value in self.seal().items() if key != "created"}
        self.assertIn("no created", "\n".join(statements_mod._seal_problems(seal)))

    def test_the_writer_refuses_a_target_inside_this_checkout(self):
        with self.assertRaises(export_records.ExportProblem) as caught:
            write_set(HERE / "fixtures" / "nope")
        self.assertIn("WikiLean is public", str(caught.exception))

    def test_the_writer_refuses_a_directory_that_is_not_an_earlier_statement_set(self):
        out = self.root / "set"
        out.mkdir()
        (out / "notes.txt").write_text("hello", encoding="utf-8")
        with self.assertRaises(export_records.ExportProblem) as caught:
            write_set(out)
        self.assertIn("holds no statement set seal", str(caught.exception))

    def test_a_re_export_removes_what_the_new_set_does_not_carry(self):
        out = self.root / "set"
        write_set(out)
        rows = inventory()
        rows.pop("enwiki-1007-3008")
        write_set(out, inventory_rows=rows)
        self.assertFalse((out / "statements/enwiki-1007-3008.jsonl").exists())
        self.assertEqual(sorted(tree(out)),
                         sorted(["SEAL.json", f"statements/{FIRST}.jsonl", "sources.jsonl"]))

    def test_the_pins_are_held_to_the_record_sets_grammar(self):
        for over, expected in (({"set_id": "has space"}, "--set-id"),
                               ({"campaign": "run:"}, "campaign"),
                               ({"created": "2026-11-12"}, "created"),
                               ({"producer_commit": "ab"}, "producer commit"),
                               ({"sourceset_manifest": "ab"}, "--sourceset-manifest"),
                               ({"span_end_rule": 0}, "--span-end-rule"),
                               ({"statement_cut_rule": "4"}, "--statement-cut-rule")):
            with self.subTest(over=sorted(over)):
                with self.assertRaises((export_records.ExportProblem,
                                        export_statements.StatementExportProblem)) as caught:
                    write_set(self.root / f"set-{len(str(over))}-{sorted(over)[0]}", **over)
                self.assertIn(expected, str(caught.exception))


# ---- the refusals ------------------------------------------------------------------------------


class RefusalTests(Temp):
    """One case per rule a statement is held to, each starting from the good cut.

    Every case is run **twice**: once through the writer, which projects a harvested
    passage into the sealed shape, and once through the reader's own judgement of the
    sealed bytes. The two are one function (``statements.statement_problems``) and these
    tests are what holds them to being one.
    """

    #: ``(name, what to change on the statement, a phrase of the writer's refusal,
    #: a phrase of the reader's where the two differ)``
    CASES = (
        ("no item", {"item": None}, "item None is no", "no item"),
        ("an item outside the head rule's grammar", {"item": "In-text #zz"},
         "is no 'In-text #<8 to 16 hex>'", None),
        ("a span outside the layer", {"span": [0, 100000]}, "a span is [start, end]",
         "leaves the pinned layer"),
        ("a backwards span", {"span": [40, 10]}, "a span is [start, end]",
         "is no [start, end] with 0 <= start < end"),
        ("no span", {"span": None}, "a span is [start, end]", "no span"),
        ("no quote", {"quote": {}}, "quote is {exact, prefix, suffix}", None),
        ("a quote over the word cap",
         {"quote": {"exact": "A sprocket bundle is a widget equipped with a chosen sprocket over "
                             "each handle.", "prefix": "", "suffix": ""}}, "over the cap of 12",
         None),
        ("an affix over the character cap",
         {"quote": {"exact": "A sprocket bundle is a widget equipped with a chosen sprocket over",
                    "prefix": "", "suffix": "x" * 33}}, "over the cap of 32", None),
        ("a quote that is not on the layer",
         {"quote": {"exact": "A cog bundle is", "prefix": "", "suffix": ""}},
         "the quote is not the one text at 0 on the pinned layer", None),
        ("a quote that is not at the span's start",
         {"span": [80, 179]}, "the quote is not the one text at 80 on the pinned layer", None),
        ("a quote that runs past the end of the span", {"span": [0, 5]},
         "the quote runs past the end of the span", None),
        ("no head_words", {"head_words": None}, "head_words is", "no head_words"),
        ("a kind outside the vocabulary", {"passage_kind": "lemma"}, "passage_kind 'lemma'",
         None),
        ("a prominence outside the vocabulary", {"prominence": "headline"},
         "prominence 'headline'", None),
        ("a scope outside the vocabulary", {"scope": "maths"}, "scope 'maths'", None),
        ("a finder outside the vocabulary", {"finder": "hunch"}, "finder 'hunch'", None),
        ("a non-formal passage that is not supporting",
         {"scope": "expository", "prominence": "subject"}, "always supporting", None),
        ("a formal_math passage with no restatement", {"statement": "   "},
         "carries its restatement", None),
        ("a restatement over the character cap", {"statement": "x" * 1001},
         "over the cap of 1000", None),
        # Everything written from a statement is UTF-8 — a record's canonical JSON, the
        # store's view model — and a lone surrogate is text UTF-8 cannot carry. It is
        # named here rather than raised by whichever writer meets it first.
        ("a restatement holding a lone surrogate",
         {"statement": "A sprocket bundle \ud800 is one."},
         "a character UTF-8 cannot carry", None),
        ("a restatement that quotes the source past the cap",
         {"statement": "A sprocket bundle is a widget equipped with a chosen sprocket over each "
                       "handle."}, "past the 12-word quote cap", None),
        ("no section", {"section": None}, "section is the heading path", "no section"),
        ("a section with an empty heading", {"section": [""]}, "section is the heading path",
         None),
        ("an empty fragment", {"fragment": ""}, "fragment ''", None),
        ("a padded fragment", {"fragment": "  Notation  "}, "fragment '  Notation  '", None),
        ("a shape with no conjuncts", {"shape": "cases"}, "ride together", None),
        ("conjuncts with no shape",
         {"conjuncts": [{"id": "c1", "text": "one"}, {"id": "c2", "text": "two"}]},
         "ride together", None),
    )
    #: The same, on the compound statement (the second of the first source).
    COMPOUND_CASES = (
        ("one conjunct", {"conjuncts": [{"id": "c1", "text": "one"}]}, "1 conjuncts", None),
        ("six conjuncts", {"conjuncts": [{"id": f"c{n}", "text": str(n)} for n in range(6)]},
         "6 conjuncts", None),
        ("two conjuncts with one id",
         {"conjuncts": [{"id": "c1", "text": "one"}, {"id": "c1", "text": "two"}]},
         "share an id", None),
        ("a shape outside the vocabulary", {"shape": "mixture"}, "shape 'mixture'", None),
        ("a shape with the wrong number of conjuncts",
         {"conjuncts": [{"id": "c1", "text": "one"}, {"id": "c2", "text": "two"},
                        {"id": "c3", "text": "three"}]}, "exactly 2 conjuncts", None),
        ("a conjunct with a part it cannot have yet",
         {"conjuncts": [{"id": "c1", "text": "one", "part": "x" * 64},
                        {"id": "c2", "text": "two"}]}, "{id, text} and nothing else", None),
        ("a conjunct with no text",
         {"conjuncts": [{"id": "c1", "text": " "}, {"id": "c2", "text": "two"}]},
         "the text non-empty", None),
        ("a conjunct with a padded id",
         {"conjuncts": [{"id": " c1 ", "text": "one"}, {"id": "c2", "text": "two"}]},
         "the id a bare token", None),
        ("a conjunct over the character cap",
         {"conjuncts": [{"id": "c1", "text": "x" * 1001}, {"id": "c2", "text": "two"}]},
         "over the cap of 1000", None),
        ("a conjunct that quotes the source past the cap",
         {"conjuncts": [{"id": "c1", "text": "Every finite sprocket bundle has a trivialising "
                                             "cover, and any two trivialising covers"},
                        {"id": "c2", "text": "two"}]}, "past the 12-word quote cap", None),
        ("conjuncts on a passage that is not formal_math",
         {"scope": "expository", "prominence": "supporting"}, "conjuncts on a 'expository'",
         None),
    )
    #: Which keys a change of None drops rather than sets.
    DROPPED = ("item", "span", "head_words", "section", "statement")

    def apply(self, row: dict, change: dict) -> dict:
        out = dict(row)
        for key, value in change.items():
            if value is None and key in self.DROPPED:
                out.pop(key, None)
            else:
                out[key] = value
        return out

    def refuse(self, index: int, change: dict) -> list[dict]:
        rows = inventory()
        rows[FIRST][index] = self.apply(rows[FIRST][index], change)
        # a fresh target a case at a time: a shared one would export over the last set,
        # so every case but the first would be exercising the re-export path
        self._case = getattr(self, "_case", 0) + 1
        return write_set(self.root / f"set-{index}-{self._case}",
                         inventory_rows=rows)["refused"]

    def reader_problems(self, index: int, change: dict) -> list[str]:
        """What the reader makes of the same change, judged over the sealed shape."""
        sealed = statements_mod.read_set(FIXTURE).statements[FIRST][index]
        return statements_mod.statement_problems(
            self.apply(sealed, change), 1005, "at", layer=LAYERS[FIRST])

    def check(self, index: int, cases) -> None:
        for name, change, expected, reader_expected in cases:
            with self.subTest(case=name, side="writer"):
                refused = self.refuse(index, change)
                self.assertEqual(len(refused), 1, refused)
                self.assertIn(expected, refused[0]["reason"])
            with self.subTest(case=name, side="reader"):
                problems = self.reader_problems(index, change)
                self.assertTrue(problems, f"the reader accepted {name}")
                self.assertIn(reader_expected or expected, "\n".join(problems))

    def test_every_rule_refuses_the_statement_that_breaks_it(self):
        self.check(0, self.CASES)

    def test_every_compound_rule_refuses_the_statement_that_breaks_it(self):
        self.check(1, self.COMPOUND_CASES)

    def test_a_statement_that_is_not_an_object_is_refused_not_a_crash(self):
        rows = inventory()
        rows[FIRST].append("not a passage")
        refused = write_set(self.root / "set", inventory_rows=rows)["refused"]
        self.assertEqual(len(refused), 1)
        self.assertIsNone(refused[0]["item"])
        self.assertIn("a cut passage is an object", refused[0]["reason"])
        self.assertIn("a statement is an object",
                      "\n".join(statements_mod.statement_problems("x", 1005, "at")))

    def test_a_cut_that_seals_nothing_is_refused_before_anything_is_written(self):
        out = self.root / "set"
        with self.assertRaises(export_statements.StatementExportProblem) as caught:
            write_set(out, inventory_rows={})
        self.assertIn("no statement to seal", str(caught.exception))
        self.assertFalse(out.exists())

    def test_the_rest_of_the_set_is_written_and_the_run_exits_one(self):
        rows = inventory()
        rows[FIRST][0]["passage_kind"] = "lemma"
        out = self.root / "set"
        report = write_set(out, inventory_rows=rows)
        self.assertEqual(report["exported"], {"sources": 2, "statements": 7})
        self.assertEqual(len(statements_mod.read_set(out).statements[FIRST]), 4)

    def test_two_statements_with_one_span_text_are_refused(self):
        rows = inventory()
        rows[FIRST].append({**rows[FIRST][0], "item": "In-text #abcdef01"})
        refused = write_set(self.root / "set", inventory_rows=rows)["refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn("one item or one span text", refused[0]["reason"])

    def test_two_statements_sharing_a_character_are_refused(self):
        rows = inventory()
        # the first passage runs into the second: both quotes still sit at their spans
        rows[FIRST][0]["span"] = [rows[FIRST][0]["span"][0], rows[FIRST][1]["span"][0] + 5]
        refused = write_set(self.root / "set", inventory_rows=rows)["refused"]
        self.assertTrue(any("shares a character" in case["reason"] for case in refused), refused)

    def test_a_second_subject_statement_is_refused(self):
        rows = inventory()
        rows[FIRST][3]["prominence"] = "subject"
        refused = write_set(self.root / "set", inventory_rows=rows)["refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn("a second subject passage", refused[0]["reason"])

    def test_a_cut_that_seals_nothing_is_refused_before_anything_is_written(self):
        out = self.root / "set"
        with self.assertRaises(export_statements.StatementExportProblem) as caught:
            write_set(out, inventory_rows={})
        self.assertIn("no statement to seal", str(caught.exception))
        self.assertFalse(out.exists())

    def test_a_source_with_no_rendered_layer_is_refused_whole(self):
        refused = write_set(self.root / "set",
                            layers={FIRST: LAYERS[FIRST]})["refused"]
        self.assertEqual([case["source_id"] for case in refused], ["enwiki-1007-3008"])
        self.assertIn("no rendered text layer", refused[0]["reason"])

    def test_a_layer_that_is_not_the_pinned_one_is_refused_whole(self):
        layers = {**LAYERS, FIRST: LAYERS[FIRST] + " And one more sentence."}
        refused = write_set(self.root / "set", layers=layers)["refused"]
        self.assertEqual([case["source_id"] for case in refused], [FIRST])
        self.assertIn("cut was made against another layer", refused[0]["reason"])

    def test_a_source_the_sourceset_does_not_pin_is_refused_whole(self):
        refused = write_set(self.root / "set",
                            sources=[fixture.sources()[0]])["refused"]
        self.assertEqual([case["source_id"] for case in refused], ["enwiki-1007-3008"])
        self.assertIn("no row in the sources given", refused[0]["reason"])

    def test_one_source_twice_or_one_page_twice_is_refused_whole(self):
        twice = [*fixture.sources(), fixture.sources()[0]]
        refused = write_set(self.root / "a", sources=twice)["refused"]
        self.assertEqual([case["reason"] for case in refused],
                         [f"{FIRST} is given twice"])
        # a second revision of the same page: two valid source ids, one page
        other = dict(fixture.sources()[1], pageid=1005, revision=9999,
                     source_id="enwiki-1005-9999")
        refused = write_set(self.root / "b", sources=[fixture.sources()[0], other],
                            layers={**LAYERS, "enwiki-1005-9999":
                                    LAYERS["enwiki-1007-3008"]})["refused"]
        self.assertTrue(any("a set holds one revision of a page" in case["reason"]
                            for case in refused), refused)

    def test_a_source_whose_identity_does_not_add_up_is_refused_whole(self):
        sources = [dict(row) for row in fixture.sources()]
        sources[0]["pageid"] = 4242
        refused = write_set(self.root / "set", sources=sources)["refused"]
        self.assertTrue(any("does not agree with pageid" in case["reason"]
                            for case in refused), refused)

    def test_a_reader_refuses_a_statement_the_writer_would_have(self):
        """The reader is not the writer: it checks the same rules over sealed bytes."""
        out = self.root / "set"
        write_set(out)
        path = out / f"statements/{FIRST}.jsonl"
        lines = path.read_text(encoding="utf-8").split("\n")
        row = json.loads(lines[0])
        row["scope"] = "maths"
        lines[0] = keys.canonical(row)
        path.write_text("\n".join(lines), encoding="utf-8")
        seal = json.loads((out / "SEAL.json").read_text(encoding="utf-8"))
        seal["files"][f"statements/{FIRST}.jsonl"] = keys.sha256_file(path)
        (out / "SEAL.json").write_text(keys.canonical_line(seal), encoding="utf-8")
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(out)
        self.assertIn("scope 'maths'", str(caught.exception))


# ---- what the reader refuses that no writer could have produced --------------------------


class ReaderTests(Temp):
    """The reader is the gate a set arriving from anywhere else passes through.

    A statement set is bytes; nothing says the bytes came from this writer. These are the
    rules that only the reader can enforce, each over a re-sealed fixture.
    """

    STATEMENTS = f"statements/{FIRST}.jsonl"

    def reseal(self, files: dict | None = None) -> Path:
        """The fixture with some files replaced, re-sealed so the seal still holds."""
        out = self.root / f"set-{len(list(self.root.iterdir()))}"
        shutil.copytree(FIXTURE, out)
        seal = json.loads((out / "SEAL.json").read_text(encoding="utf-8"))
        for rel, text in (files or {}).items():
            path = out / rel
            if text is None:
                path.unlink()
                seal["files"].pop(rel, None)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            seal["files"][rel] = keys.sha256_text(text)
        (out / "SEAL.json").write_text(keys.canonical_line(seal), encoding="utf-8")
        return out

    def refused(self, files: dict) -> str:
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(self.reseal(files))
        return str(caught.exception)

    def rows(self, name: str) -> list[dict]:
        return [json.loads(line) for line
                in (FIXTURE / name).read_text(encoding="utf-8").split("\n") if line.strip()]

    def written(self, rows: list[dict]) -> str:
        return "".join(keys.canonical_line(row) for row in rows)

    def test_a_line_that_is_not_an_object_or_not_json_is_refused_by_line(self):
        for text, expected in (("{\n", "not JSON"), ("[]\n", "not an object"),
                               ("   \n" + self.written(self.rows(self.STATEMENTS)),
                                "a blank line")):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.refused({self.STATEMENTS: text}))

    def test_a_statement_with_a_stray_or_missing_key_is_refused(self):
        rows = self.rows(self.STATEMENTS)
        stray = [{**rows[0], "text_span": [0, 10]}, *rows[1:]]
        self.assertIn("stray text_span", self.refused({self.STATEMENTS: self.written(stray)}))
        gone = [{k: v for k, v in rows[0].items() if k != "finder"}, *rows[1:]]
        self.assertIn("no finder", self.refused({self.STATEMENTS: self.written(gone)}))

    def test_two_statements_with_one_item_or_one_key_are_refused(self):
        rows = self.rows(self.STATEMENTS)
        self.assertIn("one item or one span text",
                      self.refused({self.STATEMENTS: self.written([*rows, rows[0]])}))

    def test_a_second_subject_statement_is_refused(self):
        rows = self.rows(self.STATEMENTS)
        rows[3]["prominence"] = "subject"
        self.assertIn("a second subject statement",
                      self.refused({self.STATEMENTS: self.written(rows)}))

    def test_statements_out_of_span_order_are_refused(self):
        rows = self.rows(self.STATEMENTS)
        out_of_order = [rows[1], rows[0], *rows[2:]]
        self.assertIn("written by span start, then item",
                      self.refused({self.STATEMENTS: self.written(out_of_order)}))

    def test_two_statements_sharing_a_character_are_refused(self):
        rows = self.rows(self.STATEMENTS)
        rows[1]["span"] = [rows[0]["span"][1] - 1, rows[1]["span"][1]]
        rows[1]["span_sha256"] = keys.span_sha256(LAYERS[FIRST], rows[1]["span"])
        rows[1]["passage_key"] = keys.passage_key(1005, rows[1]["span_sha256"])
        self.assertIn("shares a character",
                      self.refused({self.STATEMENTS: self.written(rows)}))

    def test_a_source_row_with_no_statement_is_refused(self):
        self.assertIn("a listed source has no statement",
                      self.refused({self.STATEMENTS: ""}))

    def test_a_statements_file_for_no_source_row_is_refused(self):
        rows = self.rows("sources.jsonl")
        self.assertIn("sources.jsonl and statements/ and nothing else",
                      self.refused({"sources.jsonl": self.written(rows[:1])}))

    def test_a_source_row_the_grammar_refuses_is_named(self):
        rows = self.rows("sources.jsonl")
        for change, expected in (({"pageid": 4242}, "does not agree with pageid"),
                                 ({"layer_sha256": "nope"}, "layer_sha256 is not 64"),
                                 ({"renderer": "a renderer"}, "is no renderer id"),
                                 ({"extra": 1}, "a source row is exactly"),
                                 ({"html_sha256": "nope"}, "html_sha256 is not 64")):
            with self.subTest(change=sorted(change)):
                written = self.written([{**rows[0], **change}, rows[1]])
                self.assertIn(expected, self.refused({"sources.jsonl": written}))

    def test_one_source_row_twice_or_one_page_twice_is_refused(self):
        rows = self.rows("sources.jsonl")
        self.assertIn("appears twice",
                      self.refused({"sources.jsonl": self.written([*rows, rows[0]])}))
        other = {**rows[1], "pageid": 1005, "revision": 9999,
                 "source_id": "enwiki-1005-9999"}
        self.assertIn("a set holds one revision of a page",
                      self.refused({"sources.jsonl": self.written([rows[0], other])}))

    def test_a_set_with_no_sources_file_in_its_seal_is_refused(self):
        out = self.reseal()
        seal = json.loads((out / "SEAL.json").read_text(encoding="utf-8"))
        seal["files"].pop("sources.jsonl")
        (out / "sources.jsonl").unlink()
        (out / "SEAL.json").write_text(keys.canonical_line(seal), encoding="utf-8")
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.read_set(out)
        self.assertIn("names no sources.jsonl", str(caught.exception))

    def test_a_render_the_set_cannot_be_checked_against_is_named(self):
        sources = render(self.root / "sources")
        (sources / FIRST / "layer_map.json").unlink()
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.frozen_inventory(FIXTURE, sources)
        self.assertIn("layer_map.json is missing", str(caught.exception))
        (sources / FIRST / "layer.txt").unlink()
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            statements_mod.frozen_inventory(FIXTURE, sources)
        self.assertIn("layer.txt is missing", str(caught.exception))

    def test_a_cut_is_handed_on_only_once_a_render_has_checked_it(self):
        st = statements_mod.read_set(FIXTURE)
        with self.assertRaises(statements_mod.StatementSetProblem) as caught:
            st.cuts()
        self.assertIn("have not been checked against a render", str(caught.exception))
        statements_mod.frozen_inventory(st, render(self.root / "sources"))
        cuts = st.cuts()
        self.assertEqual(len(cuts), 8)
        first = st.statements[FIRST][0]
        self.assertEqual(cuts[(FIRST, first["item"])],
                         records_mod.Cut(first["quote"], first["span_sha256"]))


# ---- the command line ------------------------------------------------------------------------


class CommandTests(Temp):
    """The CLI path: a sourceset and a harvested cut run, sealed as a statement set."""

    def workspace(self) -> tuple[Path, Path]:
        ss = sourceset_mod.Sourceset.create(self.root / "ss-test", "ss-test", "enwiki",
                                            created="2026-01-01T00:00:00Z")
        entries = {}
        for entry in fixture.sources():
            entries[entry["source_id"]] = {
                **entry, "family": "enwiki", "fetch": None,
                "flags": {"biography": False, "disambiguation": False, "list": False,
                          "redirect": False},
                "licence_class": "open_share_alike", "origin": "adopted", "raw_sha256": None}
        ss.write_version(entries, created="2026-01-02T00:00:00Z")
        render(ss.dir / "sources")
        corpus = self.root / "corpus" / "inv-test-1"
        for sid, rows in inventory().items():
            path = corpus / "inv-test-1-t1" / f"{sid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_INVENTORY,
                 "run_id": "inv-test-1", "task_id": "inv-test-1-t1", "source_id": sid,
                 "layer_sha256": keys.sha256_text(LAYERS[sid]),
                 "renderer": vocab.RENDERER_WEB_HTML, "passages": rows},
                sort_keys=True), encoding="utf-8")
        return ss.dir, corpus

    def test_a_harvested_cut_beside_its_sourceset_seals(self):
        ss_dir, corpus = self.workspace()
        out = self.root / "out"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = export_statements.main([
                "--inventory", str(corpus), "--sourceset", str(ss_dir), "--out", str(out),
                "--set-id", "cli-test", "--campaign", "run:inv-test-1",
                "--producer-commit", "ab" * 20, "--created", "2026-11-12T01:02:03Z"])
        self.assertEqual(code, 0, buffer.getvalue())
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["exported"], {"sources": 2, "statements": 8})
        self.assertEqual(report["cut_rules"],
                         {"span_end_rule": export_statements.DEFAULT_SPAN_END_RULE,
                          "statement_cut_rule": export_statements.DEFAULT_STATEMENT_CUT_RULE})
        st = statements_mod.read_set(out)
        self.assertEqual(st.seal["sourceset"]["id"], "ss-test")
        self.assertEqual(
            {sid: [row["passage_key"] for row in rows] for sid, rows in st.statements.items()},
            {sid: [row["passage_key"] for row in rows]
             for sid, rows in statements_mod.read_set(FIXTURE).statements.items()},
            "the same cut of the same page keys the same, whatever the set's pins")
        # the spans check against the sourceset's own render
        statements_mod.frozen_inventory(st, ss_dir / "sources")

    def test_a_cut_the_run_does_not_hold_is_named(self):
        ss_dir, corpus = self.workspace()
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = export_statements.main([
                "--inventory", str(corpus), "--sourceset", str(ss_dir),
                "--out", str(self.root / "out"), "--set-id", "cli-test",
                "--campaign", "c1", "--producer-commit", "ab" * 20,
                "--created", "2026-11-12T01:02:03Z", "--source", "enwiki-5-6"])
        self.assertEqual(code, 2)
        self.assertIn("the cut holds no document for enwiki-5-6", buffer.getvalue())

    def test_a_refused_statement_exits_one_and_the_set_holds_the_rest(self):
        ss_dir, corpus = self.workspace()
        path = corpus / "inv-test-1-t1" / f"{FIRST}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["passages"][0]["finder"] = "hunch"
        path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        out = self.root / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            code = export_statements.main([
                "--inventory", str(corpus), "--sourceset", str(ss_dir), "--out", str(out),
                "--set-id", "cli-test", "--campaign", "c1",
                "--producer-commit", "ab" * 20, "--created", "2026-11-12T01:02:03Z"])
        self.assertEqual(code, 1)
        self.assertEqual(len(statements_mod.read_set(out).statements[FIRST]), 4)

    def test_the_reader_runs_from_the_command_line(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = statements_mod.main(["--statement-set", str(FIXTURE), "--verify-only"])
        self.assertEqual(code, 0)
        self.assertIn("8 statement(s)", buffer.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(statements_mod.main(
                ["--statement-set", str(FIXTURE), "--sources",
                 str(render(self.root / "sources"))]), 0)

    def test_the_reader_refuses_to_pass_a_set_it_did_not_check_against_a_render(self):
        with contextlib.redirect_stderr(io.StringIO()) as buffer:
            with self.assertRaises(SystemExit) as caught:
                statements_mod.main(["--statement-set", str(FIXTURE)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--sources names the sourceset's rendered sources",
                      buffer.getvalue())

    def test_a_set_the_reader_refuses_exits_two_from_the_command_line(self):
        out = self.root / "broken"
        shutil.copytree(FIXTURE, out)
        (out / "sources.jsonl").write_text("{}\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as buffer:
            code = statements_mod.main(["--statement-set", str(out), "--verify-only"])
        self.assertEqual(code, 2)
        self.assertIn("sha256 does not match the seal", buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
