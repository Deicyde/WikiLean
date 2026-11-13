#!/usr/bin/env python3
"""Tests for scripts/pipeline/gate/ — stage 7, the out-of-tree locate gate.

New code. Everything here is synthetic: a one-source corpus built in a temporary
directory, with made-up declarations in a made-up universe. WikiLean is public, so no
article text and no batch-1 data is committed; the batch-1 re-gate is an operator run and
its result is recorded in docs/PIPELINE.md section 8.

Two things are held:

* the text layer reproduces what the head rule and the span-end rule give — the ids and
  the ends a planner wrote — which is what makes gate rule 2 a check and not a rewrite;
* **every rule fires on a case that breaks it**, and a document that breaks none passes.
  One test per rule, each starting from the same good document and changing one thing.

Run: python3 scripts/pipeline/gate/test_gate.py
"""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
for _candidate in (HERE, HERE.parent, HERE.parents[2] / "site"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import locate_text as lt  # noqa: E402
import verify_locate as gate  # noqa: E402
import vocab  # noqa: E402

COMMIT = "b" * 40
PAGEID = 9191
SOURCE = f"enwiki-{PAGEID}-1000000007"
RUN = "loc-test-1"
INV_RUN = "inv-test-1"
CHL_RUN = "chl-test-1"
TASK = "loc-test-1-t1"
INV_TASK = "inv-test-1-t1"
CHL_TASK = "chl-0001"

#: Four blocks: a lead that defines, a compound statement, a display, and prose.
BLOCK_TEXTS = [
    "A sprocket is a gadget that turns a widget.",
    "Every sprocket is finite, and every finite gadget is a sprocket.",
    "S = W",
    "Sprockets were first described in the nineteenth century by an unnamed engineer.",
]
LAYER = " ".join(BLOCK_TEXTS)


def _blocks() -> tuple[list[dict], list[dict], list[dict]]:
    """``(paragraphs, formulas, finders)`` of the synthetic source."""
    paragraphs, at = [], 0
    for number, text in enumerate(BLOCK_TEXTS):
        span = [at, at + len(text)]
        paragraphs.append({"block": "math" if number == 2 else "p", "span": span,
                           "math": number == 2, "section": [], "formulas": int(number == 2),
                           "texhtml": 0})
        at = span[1] + 1
    display = paragraphs[2]["span"]
    formulas = [{"span": list(display), "alttext": "S = W"}]
    finders = [{"finder": "bold_definition", "section": [], "span": paragraphs[0]["span"],
                "start": paragraphs[0]["span"][0], "term": "sprocket"}]
    return paragraphs, formulas, finders


PARAGRAPHS, FORMULAS, FINDERS = _blocks()
UNIVERSE_SHA = "c" * 64
LIBRARIES = {"testlib": {"repo": "https://example.invalid/testlib", "commit": COMMIT}}
DECLS = [
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.Sprocket",
     "proof_status": "definition", "statement": False, "module": "Testlib.Defs",
     "path": "Testlib/Defs.lean", "line": 4, "end_line": 6, "kind": "structure"},
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.sprocket_finite",
     "proof_status": "proved", "statement": False, "module": "Testlib.Basic",
     "path": "Testlib/Basic.lean", "line": 11, "end_line": 13, "kind": "theorem"},
    {"library": "testlib", "commit": COMMIT, "declaration": "Testlib.finite_sprocket",
     "proof_status": "proved", "statement": False, "module": "Testlib.Basic",
     "path": "Testlib/Basic.lean", "line": 21, "end_line": 23, "kind": "theorem"},
]


def _inventory_passages(extra: int | None = None) -> list[dict]:
    """The frozen cut of the synthetic source, keyed by the head rule itself.

    ``extra`` is one more start, inside block 1: the sentence check's case, where the
    added passage starts mid-sentence and the end rule truncates the one before it. The
    compound block is then two passages, so neither carries the compound's conjuncts.
    """
    head_map = lt.HeadMap(LAYER, FORMULAS)
    blocks = lt.Blocks(LAYER, PARAGRAPHS, FORMULAS)
    rows = []
    for number, (text, para) in enumerate(zip(BLOCK_TEXTS, PARAGRAPHS)):
        if number == 2:
            continue                                   # the display is no passage of its own
        start, stop = para["span"]
        if extra is not None and start < extra < stop:
            stop = extra                  # a quote stops where the next passage starts
        quote = " ".join(LAYER[start:stop].split()[:9])
        rows.append({"start": start, "quote": quote, "number": number})
    if extra is not None:
        held = next(para for para in PARAGRAPHS if para["span"][0] <= extra < para["span"][1])
        rows.append({"start": extra,
                     "quote": " ".join(LAYER[extra:held["span"][1]].split()[:9]),
                     "number": 4})
        rows.sort(key=lambda row: row["start"])
    starts = sorted(row["start"] for row in rows)
    ids = lt.assign_ids(head_map.head, [head_map.to_head(row["start"]) for row in rows])
    out = []
    for row, (_at, digest, words) in zip(rows, ids):
        after = [start for start in starts if start > row["start"]]
        end = lt.passage_end(LAYER, blocks, row["start"], row["quote"], None, rule=2,
                             next_start=after[0] if after else None, formulas=FORMULAS)
        passage = {"item": "In-text #" + digest, "head_words": words,
                   "span": [row["start"], end], "section": [], "fragment": None,
                   "quote": {"exact": row["quote"], "prefix": "", "suffix": ""},
                   "passage_kind": "definition" if row["number"] == 0 else "theorem",
                   "prominence": "subject" if row["number"] == 0 else "supporting",
                   "scope": "expository" if row["number"] == 3 else "formal_math",
                   # what the inventory prompt asks for beside the cut: which finder
                   # proposed the passage, and the section's fragment
                   "finder": "bold_definition" if row["number"] == 0 else "model",
                   "statement": f"A restatement of block {row['number']}."}
        if row["number"] == 1 and extra is None:
            passage["conjuncts"] = [{"id": "c1", "text": "every sprocket is finite"},
                                    {"id": "c2", "text": "every finite gadget is a sprocket"}]
            passage["shape"] = "conclusions"
        if row["number"] == 3:
            passage["prominence"] = "supporting"
        out.append(passage)
    return out


INVENTORY_PASSAGES = _inventory_passages()
LEAD, COMPOUND, PROSE = INVENTORY_PASSAGES
HTML_SHA = "d" * 64
#: The bare "and" of block 1: a word of the sentence where no rule of the cut starts a
#: passage, which is what statement-cut rule 4 refuses (issue #172).
BARE_AND = LAYER.index("and every finite gadget")


def location(declaration: str, relation: str, rank: int, **extra) -> dict:
    out = {"library": "testlib", "declaration": declaration, "relation": relation,
           "rank": rank, "confidence": "high", "note": f"a note about {declaration}",
           "clauses": ["c1"], **extra}
    return out


def good_locate_document() -> dict:
    """A locate document of the synthetic task that breaks no rule."""
    return {
        "schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_LOCATE, "run_id": RUN,
        "task_id": TASK, "source_id": SOURCE, "source_sha": HTML_SHA,
        "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")), "renderer": "web-html/3",
        "universe": "u-test", "universe_sha256": UNIVERSE_SHA,
        "passages": [
            {"item": LEAD["item"], "outcome": "located",
             "locations": [location("Testlib.Sprocket", "exactMatch", 1,
                                    instantiation="at the gadget of the passage",
                                    tests={"r2": "Reading it at a gadget gives the passage.",
                                           "r3": "It asks only for a gadget and a widget."})],
             "receipt": {"queries": [], "candidates": []}},
            {"item": COMPOUND["item"], "outcome": "located",
             "locations": [
                 location("Testlib.sprocket_finite", "hasPart", 1, clauses=["c1"],
                          conjunct_relation="exactMatch", step="every sprocket is finite",
                          instantiation="at the sprockets of the passage",
                          tests={"r2": "At a sprocket it gives the first clause.",
                                 "r3": "It asks only that the gadget be a sprocket."}),
                 location("Testlib.finite_sprocket", "hasPart", 2, clauses=["c2"],
                          conjunct_relation="exactMatch", step="every finite gadget is one",
                          instantiation="at the finite gadgets of the passage",
                          tests={"r2": "At a finite gadget it gives the second clause.",
                                 "r3": "It asks only that the gadget be finite."})],
             "coverage": {"complete": True, "parts": [1, 2],
                          "note": "The two parts state both clauses of the statement."},
             "receipt": {"queries": [], "candidates": []}},
            {"item": PROSE["item"], "outcome": "absent", "locations": [],
             "receipt": {"queries": ["sprocket history", "nineteenth century gadget",
                                     "engineer sprocket"],
                         "query_clauses": {"sprocket history": ["c1"],
                                           "nineteenth century gadget": ["c1"],
                                           "engineer sprocket": ["c1"]},
                         "candidates": []}},
        ],
    }


def good_inventory_document() -> dict:
    return {
        "schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_INVENTORY, "run_id": INV_RUN,
        "task_id": INV_TASK, "source_id": SOURCE, "source_sha": HTML_SHA,
        "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")), "renderer": "web-html/3",
        # the display is nobody's passage, and rule 8 asks for a skip that says so
        "passages": copy.deepcopy(INVENTORY_PASSAGES),
        "skips": [{"span": list(PARAGRAPHS[2]["span"]),
                   "reason": "a display the statements above already state"}],
        "notes": "",
    }


#: The location keys of the four records the synthetic challenge task holds.
LEAD_KEY, PART1_KEY, PART2_KEY, BROAD_KEY = ("1" * 64, "2" * 64, "3" * 64, "4" * 64)


def _challenge_record(passage: dict, location_key: str, location: dict) -> dict:
    """One record of a challenge task manifest, as ``challenge._record`` writes it."""
    record = {"target": "location", "item": passage["item"], "run_id": RUN,
              "prominence": passage["prominence"], "scope": passage["scope"],
              "passage_kind": passage["passage_kind"], "outcome": "located",
              "span": passage["span"], "section": [], "statement": passage["statement"],
              "location_key": location_key, "library": location["library"],
              "declaration": location["declaration"], "relation": location["relation"],
              "rank": location.get("rank"), "note": location.get("note")}
    record.update({key: location[key] for key in vocab.JUDGMENT_LOCATION_FIELDS
                   if key in location})
    if passage.get("conjuncts"):
        record["conjuncts"] = passage["conjuncts"]
        record["shape"] = passage["shape"]
    return record


def challenge_records() -> list[dict]:
    """The four records of the synthetic challenge task.

    Three are the locate document's own locations — a strong one on the simple lead and
    the compound statement's two parts — and the fourth is a strong location on the
    compound statement, which is what a challenge confirming one clause of two leaves.
    """
    located = good_locate_document()["passages"]
    lead = located[0]["locations"][0]
    part1, part2 = located[1]["locations"]
    broad = location("Testlib.Sprocket", "broadMatch", 3, clauses=["c1", "c2"],
                     instantiation="at the sprockets and the finite gadgets",
                     tests={"r2": "Both clauses follow by reading it at the passage's "
                                  "gadgets.",
                            "r3": "It asks for nothing the passage does not give."})
    return [_challenge_record(LEAD, LEAD_KEY, lead),
            _challenge_record(COMPOUND, PART1_KEY, part1),
            _challenge_record(COMPOUND, PART2_KEY, part2),
            _challenge_record(COMPOUND, BROAD_KEY, broad)]


def good_challenge_document() -> dict:
    """A challenge document of the synthetic task that breaks no rule."""
    return {
        "schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_CHALLENGE, "run_id": CHL_RUN,
        "task_id": CHL_TASK, "source_id": SOURCE, "source_sha": HTML_SHA,
        "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")), "renderer": "web-html/3",
        "universe": "u-test", "universe_sha256": UNIVERSE_SHA,
        "verdicts": [
            {"item": LEAD["item"], "target": "location", "location_key": LEAD_KEY,
             "action": "confirm", "clauses": ["c1"],
             "tests": {"r2": "The structure read at a gadget is the passage.",
                       "r3": "It asks for a gadget and a widget and no more."},
             "checks": {"reading": ["a sprocket is a gadget",
                                    "a sprocket turns a widget"],
                        "states": [1, 2],
                        "kind_check": "a definition, neither notation nor an example"},
             "witness": "`structure Sprocket` is the gadget the passage defines."},
            {"item": COMPOUND["item"], "target": "location", "location_key": PART1_KEY,
             "action": "confirm", "clauses": ["c1"],
             "tests": {"r2": "At a sprocket the theorem is the first clause.",
                       "r3": "It asks only that the gadget be a sprocket."},
             "checks": {"reading": ["every sprocket is finite",
                                    "every finite gadget is a sprocket"],
                        "states": [1],
                        "kind_check": "a theorem, neither notation nor an example"},
             "witness": "The theorem states finiteness of every sprocket."},
            {"item": COMPOUND["item"], "target": "location", "location_key": PART2_KEY,
             "action": "downgrade", "relation": "relatedMatch",
             "witness": "It needs a second lemma before it reaches either clause."},
            {"item": COMPOUND["item"], "target": "location", "location_key": BROAD_KEY,
             "action": "confirm", "clauses": ["c1", "c2"],
             "tests": {"r2": "Both clauses follow by instantiation.",
                       "r3": "Neither clause needs anything the structure lacks."},
             "checks": {"reading": ["every sprocket is finite",
                                    "every finite gadget is a sprocket"],
                        "states": [1, 2],
                        "direction": {"instance": "read at the passage's gadgets",
                                      "hypotheses": "none"},
                        "kind_check": "a theorem, neither notation nor an example"},
             "witness": "The structure states both halves of the statement."},
        ],
    }


def mid_sentence_inventory_document() -> dict:
    """The good inventory with block 1 cut at its bare "and": the start statement-cut
    rule 4 refuses, and the passage the end rule truncates there."""
    doc = good_inventory_document()
    doc["passages"] = _inventory_passages(BARE_AND)
    return doc


class Fixture:
    """A staged gate directory and a one-document worktree, in a temporary directory."""

    def __init__(self, root: Path, *, mode: str = vocab.MODE_LOCATE, **gate_overrides):
        self.root = root
        self.mode = mode
        self.run_id = {vocab.MODE_LOCATE: RUN, vocab.MODE_INVENTORY: INV_RUN,
                       vocab.MODE_CHALLENGE: CHL_RUN}[mode]
        self.task_id = {vocab.MODE_LOCATE: TASK, vocab.MODE_INVENTORY: INV_TASK,
                        vocab.MODE_CHALLENGE: CHL_TASK}[mode]
        source_dir = root / "sources" / SOURCE
        source_dir.mkdir(parents=True)
        (source_dir / "layer.txt").write_text(LAYER, encoding="utf-8")
        self._json(source_dir / "paragraphs.json", PARAGRAPHS)
        self._json(source_dir / "formulas.json", FORMULAS)
        self._json(source_dir / "finders.json", FINDERS)
        (root / "candidates").mkdir()
        self._json(root / "candidates" / f"{SOURCE}.json",
                   {"source_id": SOURCE, "curated": [], "lexical": [], "dropped": []})
        (root / "decls.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in DECLS), encoding="utf-8")
        (root / "tasks").mkdir()
        document = {"source_id": SOURCE, "curated": [],
                    "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")),
                    "passages": copy.deepcopy(INVENTORY_PASSAGES)}
        if mode == vocab.MODE_CHALLENGE:
            # a challenge task manifest holds records, not passages
            document = {"source_id": SOURCE, "source_sha": HTML_SHA,
                        "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")),
                        "renderer": "web-html/3", "records": challenge_records()}
        self._json(root / "tasks" / f"{self.task_id}.json", {
            "task_id": self.task_id, "run_id": self.run_id, "mode": mode,
            "documents": [document]})
        self.gate_path = root / "gate.json"
        self._json(self.gate_path, self.gate_json(**gate_overrides))
        self.findings = root / "findings" / self.task_id
        self.findings.mkdir(parents=True)
        self.document_path = self.findings / f"{SOURCE}.json"
        self.write({vocab.MODE_LOCATE: good_locate_document,
                    vocab.MODE_INVENTORY: good_inventory_document,
                    vocab.MODE_CHALLENGE: good_challenge_document}[mode]())

    @staticmethod
    def _json(path: Path, value) -> None:
        path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def gate_json(self, **overrides) -> dict:
        data = {
            "schema": gate.GATE_SCHEMA, "run_id": self.run_id, "mode": self.mode,
            "universe": {"id": "u-test", "universe_sha256": UNIVERSE_SHA,
                         "libraries": LIBRARIES},
            "sources": {SOURCE: {"html_sha256": HTML_SHA,
                                 "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")),
                                 "renderer": "web-html/3"}},
            "sources_dir": str(self.root / "sources"),
            "tasks_dir": str(self.root / "tasks"),
            "candidates_dir": str(self.root / "candidates"),
            "candidates_sha256": {},
            "findings_dir": "findings",
            "blind": False, "max_locations": 5, "min_absence_queries": 3,
            "span_end_rule": 2, "statement_cut_rule": 2, "judgment_rules": 3,
            "index": {"path": str(self.root / "decls.jsonl")},
        }
        data.update(overrides)
        return data

    def write(self, document: dict) -> None:
        self._json(self.document_path, document)

    def set_gate(self, **overrides) -> None:
        self._json(self.gate_path, self.gate_json(**overrides))

    def set_records(self, records: list) -> None:
        """Rewrite the challenge task manifest's records."""
        self.set_task({"task_id": self.task_id, "run_id": self.run_id, "mode": self.mode,
                       "documents": [{"source_id": SOURCE, "source_sha": HTML_SHA,
                                      "layer_sha256": gate.sha256_bytes(
                                          LAYER.encode("utf-8")),
                                      "renderer": "web-html/3", "records": records}]})

    def set_candidates(self, value: dict) -> None:
        self._json(self.root / "candidates" / f"{SOURCE}.json", value)

    def set_task(self, value: dict) -> None:
        self._json(self.root / "tasks" / f"{self.task_id}.json", value)

    def run(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--gate-config", str(self.gate_path), "--root", str(self.root), *args]
        if not any(flag in args for flag in ("--findings", "--changed-files",
                                             "--findings-dir", "--self-check")):
            argv += ["--findings-dir", str(self.root / "findings")]
        with redirect_stdout(out), redirect_stderr(err):
            code = gate.main(argv, env={"TEND_TASK_ID": self.task_id})
        return code, out.getvalue() + err.getvalue()


class TextLayerTests(unittest.TestCase):
    """The head rule and the span-end rule, which gate rule 2 recomputes."""

    def test_the_head_rule_keys_every_passage_of_the_source_together(self):
        head_map = lt.HeadMap(LAYER, FORMULAS)
        starts = [head_map.to_head(p["span"][0]) for p in INVENTORY_PASSAGES]
        ids = lt.assign_ids(head_map.head, starts)
        for passage, (_at, digest, words) in zip(INVENTORY_PASSAGES, ids):
            self.assertEqual(passage["item"], "In-text #" + digest)
            self.assertEqual(passage["head_words"], words)

    def test_a_formula_is_a_token_in_the_head_text(self):
        head_map = lt.HeadMap(LAYER, FORMULAS)
        self.assertIn(lt.formula_token("S = W"), head_map.head)
        with self.assertRaises(lt.HeadError):
            head_map.to_head(FORMULAS[0]["span"][0] + 1)

    def test_the_whitespace_rule_is_the_repositorys_own(self):
        import anchor_resolve

        self.assertEqual(lt.normalize_text("a   b\t"), anchor_resolve.collapse("a   b\t"))
        self.assertEqual(lt.normalize_text("a   b\t"), "a b")

    def test_a_quote_must_occur_exactly_once(self):
        self.assertEqual(len(lt.find_quote(LAYER, "A sprocket is a gadget")), 1)
        self.assertEqual(len(lt.find_quote(LAYER, "sprocket")), 3)
        self.assertEqual(lt.find_quote(LAYER, ""), [])


class GatePassesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(Path(self._tmp.name))

    def test_a_good_document_passes(self):
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)
        self.assertIn("PASS", text)

    def test_a_good_inventory_passes(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_OK, text)

    def test_a_good_inventory_passes_the_sentence_check_too(self):
        """Statement-cut rule 4 refuses nothing a whole-sentence cut writes."""
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            fixture.set_gate(statement_cut_rule=4)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_OK, text)

    def test_self_check_says_what_it_does_not_check(self):
        code, text = self.fx.run("--self-check")
        self.assertEqual(code, gate.EXIT_OK, text)
        self.assertIn("the gate can run", text)
        for todo in gate.TODO_CHECKS:
            self.assertIn(todo, text)

    def test_the_command_line_is_the_label(self):
        for args in (("--findings", str(self.fx.document_path), "--changed-files", "x"),
                     ("--pre-merge",)):
            with self.subTest(args=args):
                code, text = self.fx.run(*args)
                self.assertEqual(code, gate.EXIT_BROKEN, text)

    def test_a_gate_that_cannot_run_exits_three(self):
        self.fx.gate_path.write_text("{not json", encoding="utf-8")
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_BROKEN, text)
        self.assertIn("FAIL", text)
        # and a missing source is the same failure, never a rejection
        self.fx.set_gate(sources_dir=str(self.fx.root / "nowhere"))
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_BROKEN, text)


class EachRuleFiresTests(unittest.TestCase):
    """One breaking case per rule: the gate rejects, and names that rule."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(Path(self._tmp.name))

    def rejects(self, rule: int, *args: str) -> str:
        code, text = self.fx.run(*args)
        self.assertEqual(code, gate.EXIT_REJECTED, f"expected a rejection, got:\n{text}")
        self.assertIn(f"rule {rule}:", text, text)
        return text

    # -- rule 1 ---------------------------------------------------------------------

    def test_rule1_fires_on_a_document_that_is_not_the_runs(self):
        doc = good_locate_document()
        doc["run_id"] = "some-other-run"
        self.fx.write(doc)
        self.rejects(1)

    def test_rule1_fires_on_a_coverage_claim_with_one_part(self):
        doc = good_locate_document()
        doc["passages"][1]["coverage"]["parts"] = [1]
        self.fx.write(doc)
        self.rejects(1)

    def test_rule1_fires_on_a_record_the_export_would_refuse(self):
        # the index gives this declaration no module, so the record builder — the very
        # one the export runs — can write no lean end for it
        rows = [dict(row) for row in DECLS]
        rows[0]["module"] = ""
        (self.fx.root / "decls.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.rejects(1)

    # -- rule 2 ---------------------------------------------------------------------

    def test_rule2_fires_on_an_item_that_is_not_the_heads_hash(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["passages"][0]["item"] = "In-text #deadbeef"
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 2:", text)

    def test_rule2_fires_on_a_span_the_end_rule_does_not_give(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["passages"][0]["span"] = [doc["passages"][0]["span"][0],
                                          doc["passages"][0]["span"][1] - 1]
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 2:", text)

    def test_rule2_fires_on_a_passage_no_frozen_inventory_holds(self):
        doc = good_locate_document()
        doc["passages"][0]["item"] = "In-text #0badf00d"
        self.fx.write(doc)
        self.rejects(2)

    def test_rule2_fires_on_a_layer_that_is_not_the_pinned_one(self):
        doc = good_locate_document()
        doc["layer_sha256"] = "e" * 64
        self.fx.write(doc)
        self.rejects(1)        # the header pin is rule 1's; the layer's bytes are rule 2's

    # -- rule 3 ---------------------------------------------------------------------

    def test_rule3_fires_on_a_declaration_the_index_does_not_hold(self):
        doc = good_locate_document()
        doc["passages"][0]["locations"][0]["declaration"] = "Testlib.NotThere"
        self.fx.write(doc)
        self.rejects(3)

    def test_rule3_fires_on_a_worker_that_writes_the_proof_status(self):
        doc = good_locate_document()
        doc["passages"][0]["locations"][0]["proof_status"] = "proved"
        self.fx.write(doc)
        self.rejects(3)

    # -- rule 4 ---------------------------------------------------------------------

    def test_rule4_fires_on_a_curated_hit_nobody_handled(self):
        curated = [{"library": "testlib", "declaration": "Testlib.finite_sprocket"}]
        self.fx.set_candidates({"source_id": SOURCE, "curated": curated, "lexical": []})
        self.fx.set_task({"task_id": TASK, "run_id": RUN, "mode": vocab.MODE_LOCATE,
                          "documents": [{"source_id": SOURCE, "curated": curated,
                                         "passages": copy.deepcopy(INVENTORY_PASSAGES)}]})
        doc = good_locate_document()
        # the compound passage's two parts are the only places it is located; drop them
        doc["passages"][1]["locations"] = [doc["passages"][1]["locations"][0]]
        doc["passages"][1].pop("coverage")
        self.fx.write(doc)
        self.rejects(4)

    def test_rule4_fires_on_a_hit_the_pinned_candidates_file_does_not_carry(self):
        self.fx.set_task({"task_id": TASK, "run_id": RUN, "mode": vocab.MODE_LOCATE,
                          "documents": [{"source_id": SOURCE,
                                         "curated": [{"library": "testlib",
                                                      "declaration": "Testlib.Sprocket"}],
                                         "passages": copy.deepcopy(INVENTORY_PASSAGES)}]})
        self.rejects(4)

    # -- rule 5 ---------------------------------------------------------------------

    def test_rule5_fires_on_an_absence_with_too_few_queries(self):
        doc = good_locate_document()
        receipt = doc["passages"][2]["receipt"]
        receipt["queries"] = receipt["queries"][:2]
        receipt["query_clauses"] = {q: ["c1"] for q in receipt["queries"]}
        self.fx.write(doc)
        self.rejects(5)

    def test_rule5_fires_on_a_document_that_names_another_universe(self):
        doc = good_locate_document()
        doc["universe_sha256"] = "f" * 64
        self.fx.write(doc)
        self.rejects(5)

    # -- rule 6 ---------------------------------------------------------------------

    def test_rule6_fires_on_ranks_that_are_not_one_to_n(self):
        doc = good_locate_document()
        doc["passages"][1]["locations"][1]["rank"] = 3
        self.fx.write(doc)
        self.rejects(6)

    def test_rule6_fires_on_one_declaration_twice(self):
        doc = good_locate_document()
        doc["passages"][1]["locations"][1]["declaration"] = "Testlib.sprocket_finite"
        self.fx.write(doc)
        self.rejects(6)

    def test_rule6_fires_over_the_cap(self):
        self.fx.set_gate(max_locations=1)
        self.rejects(6)

    # -- rule 7 ---------------------------------------------------------------------

    def test_rule7_fires_on_a_file_outside_the_tasks_own(self):
        listing = self.fx.root / "changed.txt"
        listing.write_text(f"findings/some-other-task/{SOURCE}.json\n", encoding="utf-8")
        other = self.fx.root / "findings" / "some-other-task"
        other.mkdir()
        (other / f"{SOURCE}.json").write_text(
            json.dumps(good_locate_document()), encoding="utf-8")
        self.rejects(7, "--changed-files", str(listing))

    def test_rule7_fires_on_a_path_that_is_not_a_document(self):
        listing = self.fx.root / "changed.txt"
        listing.write_text("README.md\n", encoding="utf-8")
        (self.fx.root / "README.md").write_text("{}", encoding="utf-8")
        self.rejects(7, "--changed-files", str(listing))

    # -- rule 8 ---------------------------------------------------------------------

    def test_rule8_fires_on_a_math_paragraph_nobody_covered(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["skips"] = []        # the display is now covered by nothing
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 8:", text)

    def test_rule8_fires_on_a_skip_that_swallows_a_kept_passage(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["skips"] = [{"span": [0, len(LAYER)], "reason": "everything"}]
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 8:", text)

    # -- rule 9 ---------------------------------------------------------------------

    def test_rule9_fires_on_a_strong_location_with_no_tests(self):
        doc = good_locate_document()
        doc["passages"][0]["locations"][0].pop("tests")
        self.fx.write(doc)
        self.rejects(9)

    def test_rule9_fires_on_a_relatedmatch_that_names_a_clause(self):
        doc = good_locate_document()
        doc["passages"][0]["locations"][0].update(relation="relatedMatch", clauses=["c1"])
        self.fx.write(doc)
        self.rejects(9)

    def test_rule9_fires_on_a_convention_outside_the_closed_list(self):
        doc = good_locate_document()
        doc["passages"][0]["locations"][0].update(relation="closeMatch",
                                                  convention="handedness")
        self.fx.write(doc)
        self.rejects(9)

    def test_rule9_fires_on_a_strong_note_that_admits_a_gap(self):
        doc = good_locate_document()
        doc["passages"][0]["locations"][0]["note"] = \
            "It does not state that the gadget turns a widget."
        self.fx.write(doc)
        self.rejects(9)

    def test_rule9_fires_on_an_absence_whose_claims_share_one_query(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other))
            doc = good_locate_document()
            # make the absent passage the compound one, whose two clauses need two queries
            # three queries as written, but one query after folding, tagged with both
            # clauses: nothing searched for c1 apart from what searched for c2
            doc["passages"] = [doc["passages"][0], {
                "item": COMPOUND["item"], "outcome": "absent", "locations": [],
                "receipt": {"queries": ["sprocket finite", "Sprocket  Finite",
                                        "SPROCKET FINITE"],
                            "query_clauses": {"sprocket finite": ["c1", "c2"],
                                              "Sprocket  Finite": ["c1", "c2"],
                                              "SPROCKET FINITE": ["c1", "c2"]},
                            "candidates": []}}]
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 9:", text)
            self.assertIn("a query of its own", text)

    def test_rule9_fires_on_a_verdict_with_no_witness(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_CHALLENGE)
            fixture.write({
                "schema": vocab.DOCUMENT_SCHEMA, "mode": vocab.MODE_CHALLENGE,
                "run_id": fixture.run_id, "task_id": fixture.task_id, "source_id": SOURCE,
                "source_sha": HTML_SHA,
                "layer_sha256": gate.sha256_bytes(LAYER.encode("utf-8")),
                "renderer": "web-html/3", "universe": "u-test",
                "universe_sha256": UNIVERSE_SHA,
                "verdicts": [{"item": LEAD["item"], "target": "location",
                              "location_key": "a" * 64, "action": "confirm"}]})
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 9:", text)

    # -- rule 10 --------------------------------------------------------------------

    def test_rule10_fires_on_two_passages_that_share_text(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["passages"][1]["span"] = [doc["passages"][0]["span"][0] + 2,
                                          doc["passages"][1]["span"][1]]
            doc["passages"][1]["until"] = LAYER[doc["passages"][1]["span"][1] - 8:
                                                doc["passages"][1]["span"][1]]
            doc["passages"][1]["quote"]["exact"] = LAYER[doc["passages"][1]["span"][0]:
                                                         doc["passages"][1]["span"][0] + 24]
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 10:", text)

    def test_rule10_fires_on_a_span_that_ends_inside_a_formula(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            middle = (FORMULAS[0]["span"][0] + FORMULAS[0]["span"][1]) // 2
            doc["passages"][1]["span"] = [doc["passages"][1]["span"][0], middle]
            doc["passages"][1]["until"] = LAYER[middle - 2:middle]
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("the span ends inside a formula", text)
            # and a span that ends WHERE a formula ends is not inside it
            doc["passages"][1]["span"] = [doc["passages"][1]["span"][0],
                                          FORMULAS[0]["span"][1]]
            doc["passages"][1]["until"] = LAYER[FORMULAS[0]["span"][1] - 2:
                                                FORMULAS[0]["span"][1]]
            fixture.write(doc)
            _code, text = fixture.run()
            self.assertNotIn("ends inside a formula", text)

    def test_rule10_fires_on_a_compound_statement_with_no_shape(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["passages"][1].pop("shape")
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 10:", text)

    def test_rule10_fires_on_two_subject_passages(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["passages"][1]["prominence"] = "subject"
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 10:", text)

    def test_rule10_fires_on_a_scoped_out_passage_that_is_not_supporting(self):
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = good_inventory_document()
            doc["passages"][2]["scope"] = "expository"
            doc["passages"][2]["prominence"] = "central"
            fixture.write(doc)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 10:", text)

    def test_rule10_fires_on_a_passage_that_starts_inside_a_sentence(self):
        """Issue #172: the cut of block 1 at its bare "and", at rule 4 and not before."""
        with tempfile.TemporaryDirectory() as other:
            fixture = Fixture(Path(other), mode=vocab.MODE_INVENTORY)
            doc = mid_sentence_inventory_document()
            fragment = next(p for p in doc["passages"] if p["span"][0] == BARE_AND)
            before = next(p for p in doc["passages"]
                          if p["span"][0] == PARAGRAPHS[1]["span"][0])
            fixture.write(doc)
            fixture.set_gate(statement_cut_rule=4)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_REJECTED, text)
            self.assertIn("rule 10:", text)
            self.assertIn("inside a sentence", text)
            # the sentence is the whole fault: the two spans are disjoint and in bounds
            self.assertIn("1 problem(s)", text)
            # the refusal names the fragment AND the passage the end rule truncates
            self.assertIn(fragment["item"], text)
            self.assertIn(before["item"], text)
            self.assertLessEqual(before["span"][1], BARE_AND)
            # the cut rules before 4 do not know the check
            fixture.set_gate(statement_cut_rule=3)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_OK, text)


class ChallengeVerdictTests(unittest.TestCase):
    """Judgment rules 5: the location a verdict leaves faces every relation rule.

    Issue #175. Each case changes one thing in the good challenge document or in the
    record its verdict judges, and asserts the gate refuses it at rules 5 and passes it
    at rules 4 — a run pinned to the old rules re-gates unchanged.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(Path(self._tmp.name), mode=vocab.MODE_CHALLENGE,
                          judgment_rules=5)

    def verdict(self, document: dict, location_key: str) -> dict:
        return next(v for v in document["verdicts"]
                    if v["location_key"] == location_key)

    def refuses(self, *, says: str = "") -> str:
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_REJECTED, f"expected a rejection, got:\n{text}")
        self.assertIn("rule 9:", text, text)
        if says:
            self.assertIn(says, text, text)
        return text

    def passes_at_four(self) -> None:
        """The same document, under the rules the old runs pinned."""
        self.fx.set_gate(judgment_rules=4)
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)
        self.fx.set_gate(judgment_rules=5)

    def test_a_good_challenge_document_passes(self):
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)
        self.assertIn("PASS", text)

    def test_a_confirm_that_states_one_clause_of_two_is_refused(self):
        """Issue #175, pattern 1: the clause check a confirm never faced."""
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY)["clauses"] = ["c1"]
        self.fx.write(doc)
        self.refuses(says="states every clause")
        self.passes_at_four()

    def test_a_confirm_of_a_record_whose_note_admits_a_gap_is_refused(self):
        records = challenge_records()
        records[0]["note"] = "It does not state that the gadget turns a widget."
        self.fx.set_records(records)
        self.refuses(says="admits a clause it does not state")
        self.passes_at_four()

    def test_a_confirm_that_writes_its_own_note_over_that_one_stands(self):
        """The fix the prompt names: the note the verdict leaves is the one checked."""
        records = challenge_records()
        records[0]["note"] = "It does not state that the gadget turns a widget."
        self.fx.set_records(records)
        doc = good_challenge_document()
        self.verdict(doc, LEAD_KEY)["note"] = "It carries the widget as a field."
        self.fx.write(doc)
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)

    def test_a_blank_note_is_no_note_and_the_records_own_still_stands(self):
        """The tally takes a verdict's text field only when it is non-empty, so a
        verdict that blanks the note leaves the record's — and the gate holds that one.
        Otherwise `"note": ""` would answer the missing-clause rule with a field the
        tally throws away."""
        records = challenge_records()
        records[0]["note"] = "It does not state that the gadget turns a widget."
        self.fx.set_records(records)
        for blank in ("", "   "):
            with self.subTest(note=repr(blank)):
                doc = good_challenge_document()
                self.verdict(doc, LEAD_KEY)["note"] = blank
                self.fx.write(doc)
                self.refuses(says="admits a clause it does not state")

    def test_a_downgrade_to_a_closematch_with_no_convention_is_refused(self):
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY).update(action="downgrade", relation="closeMatch")
        self.fx.write(doc)
        self.refuses(says="names the convention")
        self.passes_at_four()

    def test_a_downgrade_to_a_haspart_with_no_step_is_refused(self):
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY).update(action="downgrade", relation="hasPart",
                                            clauses=["c1"])
        self.fx.write(doc)
        self.refuses(says="`step`")
        self.passes_at_four()

    def test_a_downgrade_to_a_relatedmatch_that_names_a_clause_is_refused(self):
        doc = good_challenge_document()
        self.verdict(doc, PART2_KEY)["clauses"] = ["c2"]
        self.fx.write(doc)
        self.refuses(says="a relatedMatch states no clause")
        self.passes_at_four()

    def test_a_downgrade_leaves_behind_what_its_new_relation_does_not_take(self):
        """A convention, a step and a conjunct relation the record carried are dropped,
        as the tally drops them: the gate refuses no field the tally throws away."""
        records = challenge_records()
        records[3].update(relation="closeMatch", convention="encoding")
        self.fx.set_records(records)
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY).update(action="downgrade", relation="broadMatch")
        self.fx.write(doc)
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)

    def test_a_manifest_with_no_records_leaves_the_verdict_rules_alone(self):
        self.fx.set_task({"task_id": CHL_TASK, "run_id": CHL_RUN,
                          "mode": vocab.MODE_CHALLENGE,
                          "documents": [{"source_id": SOURCE, "records": []}]})
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)

    def test_the_clause_ids_are_the_records_own_conjuncts(self):
        doc = good_challenge_document()
        self.verdict(doc, PART1_KEY)["clauses"] = ["c3"]
        self.fx.write(doc)
        self.refuses(says="not this passage's")

    # -- the three checks (judgment rules 5) ----------------------------------------

    def test_a_verdict_that_leaves_a_strong_relation_runs_the_checks(self):
        doc = good_challenge_document()
        self.verdict(doc, LEAD_KEY).pop("checks")
        self.fx.write(doc)
        self.refuses(says="runs the three checks")
        self.passes_at_four()

    def test_a_confirm_that_leaves_a_haspart_runs_them_too(self):
        doc = good_challenge_document()
        self.verdict(doc, PART1_KEY).pop("checks")
        self.fx.write(doc)
        self.refuses(says="runs the three checks")
        self.passes_at_four()

    def test_a_strong_relation_that_states_some_of_its_own_reading_is_refused(self):
        """Issue #175, pattern 1: the check the inventory's clause ids cannot make."""
        doc = good_challenge_document()
        self.verdict(doc, LEAD_KEY)["checks"]["states"] = [1]
        self.fx.write(doc)
        self.refuses(says="downgrade to hasPart")
        self.passes_at_four()

    def test_a_haspart_that_states_no_fact_of_the_passage_is_refused(self):
        doc = good_challenge_document()
        self.verdict(doc, PART1_KEY)["checks"]["states"] = []
        self.fx.write(doc)
        self.refuses(says="is a relatedMatch")

    def test_a_reading_that_is_no_list_of_short_lines_is_refused(self):
        doc = good_challenge_document()
        for reading in ("every sprocket is finite", [], ["a fact\nand another"],
                        [f"fact {n}" for n in range(vocab.MAX_CONJUNCTS + 1)]):
            with self.subTest(reading=reading):
                self.verdict(doc, LEAD_KEY)["checks"]["reading"] = reading
                self.fx.write(doc)
                self.refuses(says="one-line facts the passage asserts")

    def test_a_checks_block_that_fails_three_ways_is_told_all_three_at_once(self):
        """A worker answers one gate run with one rewrite, and the retry cap is what a
        second refusal costs: a malformed `reading` hides neither the direction nor the
        kind check behind it."""
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY)["checks"] = {"reading": [], "states": [1]}
        self.fx.write(doc)
        text = self.refuses(says="one-line facts the passage asserts")
        self.assertIn("a broadMatch gives `direction`", text)
        self.assertIn("`kind_check` says in one line", text)
        self.passes_at_four()

    def test_a_broadmatch_with_no_direction_is_refused(self):
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY)["checks"].pop("direction")
        self.fx.write(doc)
        self.refuses(says="a broadMatch gives `direction`")
        self.passes_at_four()

    def test_a_broadmatch_that_names_a_hypothesis_the_passage_lacks_is_refused(self):
        """Issue #175, pattern 2: the direction R2 and PD4 ask for."""
        doc = good_challenge_document()
        self.verdict(doc, BROAD_KEY)["checks"]["direction"]["hypotheses"] = \
            "the gadget must already be finite"
        self.fx.write(doc)
        self.refuses(says="at most a narrowMatch")
        self.passes_at_four()

    def test_none_is_the_word_for_no_hypothesis(self):
        doc = good_challenge_document()
        for word in ("none", "None", "none.", "None — it assumes nothing more",
                     "(none)", "none: every binder of the structure is the passage's"):
            with self.subTest(word=word):
                self.verdict(doc, BROAD_KEY)["checks"]["direction"]["hypotheses"] = word
                self.fx.write(doc)
                code, text = self.fx.run()
                self.assertEqual(code, gate.EXIT_OK, text)

    def test_a_verdict_with_no_kind_check_is_refused(self):
        """Issue #175, pattern 3: the notation and worked-example reading."""
        doc = good_challenge_document()
        self.verdict(doc, LEAD_KEY)["checks"].pop("kind_check")
        self.fx.write(doc)
        self.refuses(says="`kind_check`")
        self.passes_at_four()

    # -- the bridge the prompt promises the gate looks up -----------------------------

    def bridge(self, declaration: str) -> dict:
        doc = good_challenge_document()
        self.verdict(doc, PART2_KEY)["bridge"] = {
            "library": "testlib", "declaration": declaration,
            "why": "it carries the finiteness the part does not reach"}
        return doc

    def test_a_bridge_the_index_holds_stands(self):
        self.fx.write(self.bridge("Testlib.finite_sprocket"))
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)

    def test_a_bridge_the_index_does_not_hold_is_refused(self):
        self.fx.write(self.bridge("Testlib.NotThere"))
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_REJECTED, text)
        self.assertIn("rule 3:", text)
        self.assertIn("(bridge)", text)
        self.passes_at_four()

    def test_a_bridge_that_names_no_lemma_is_refused(self):
        doc = good_challenge_document()
        self.verdict(doc, PART2_KEY)["bridge"] = "the finiteness lemma"
        self.fx.write(doc)
        self.refuses(says="a bridge is {library, declaration, why}")

    def test_a_bridge_on_a_relation_the_tally_drops_it_from_is_left_alone(self):
        doc = good_challenge_document()
        self.verdict(doc, LEAD_KEY)["bridge"] = {"library": "testlib",
                                                 "declaration": "Testlib.NotThere",
                                                 "why": "nothing reads this"}
        self.fx.write(doc)
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)

    def test_the_checks_of_a_weaker_relation_are_extra_and_no_refusal(self):
        """A refusal would spend a model call removing a field that harms nothing."""
        doc = good_challenge_document()
        self.verdict(doc, PART2_KEY)["checks"] = {
            "reading": ["every sprocket is finite"], "states": [],
            "direction": {"instance": "none", "hypotheses": "none"},
            "kind_check": "a theorem"}
        self.fx.write(doc)
        code, text = self.fx.run()
        self.assertEqual(code, gate.EXIT_OK, text)


#: The two readings the battery below writes of the synthetic source's two passages,
#: and the fields a verdict of it carries. All invented here.
SIMPLE_READING = ["a sprocket is a gadget", "a sprocket turns a widget"]
COMPOUND_READING = ["every sprocket is finite", "every finite gadget is a sprocket"]
TESTS = {"r2": "Reading it at the passage's gadgets gives the passage.",
         "r3": "It asks for nothing the passage does not give."}
DIRECTION = {"instance": "read at the passage's own gadgets", "hypotheses": "none"}


def checks(reading: list, states: list, direction: dict | None = None,
           kind: str = "neither a notation passage nor a worked example") -> dict:
    out = {"reading": list(reading), "states": list(states), "kind_check": kind}
    if direction is not None:
        out["direction"] = direction
    return out


def legitimate_verdicts() -> list[tuple[str, dict, dict, dict]]:
    """``(what it is, the record's location, the verdict, the passage)``.

    Verdicts a challenger may legally write, of every relation and every shape the
    rules admit: a record whose fields the verdict replaces and one whose fields it
    inherits, a part held by each conjunct relation, a downgrade to each weaker
    relation, and the two ways out of a strong relation. None of them may be refused —
    every refusal costs a model call on every run that trips it.
    """
    simple, compound = LEAD, COMPOUND
    both = ["c1", "c2"]
    return [
        ("a confirm of an exactMatch on a simple passage",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING[:1], [1])}, simple),
        ("a confirm whose own reading is finer than the inventory's one clause",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a confirm of a closeMatch with the record's own convention",
         location("Testlib.Sprocket", "closeMatch", 1, convention="encoding",
                  note="It carries the widget as an index.", instantiation="at a gadget",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a confirm of a broadMatch that instantiates to the passage",
         location("Testlib.Sprocket", "broadMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2], DIRECTION)}, simple),
        ("a confirm of a broadMatch whose hypotheses line ends in a full stop",
         location("Testlib.Sprocket", "broadMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2],
                           {**DIRECTION, "hypotheses": "None."})}, simple),
        ("a confirm of a narrowMatch, which runs no checks",
         location("Testlib.Sprocket", "narrowMatch", 1, step="the gadget case alone"),
         {"action": "confirm", "clauses": ["c1"]}, simple),
        ("a confirm of a relatedMatch, which names no clause",
         location("Testlib.Sprocket", "relatedMatch", 1, clauses=[]),
         {"action": "confirm"}, simple),
        ("a confirm of a hasPart of a simple passage, held exactly",
         location("Testlib.sprocket_finite", "hasPart", 1, conjunct_relation="exactMatch",
                  step="it gives the first half", instantiation="at a sprocket",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1])}, simple),
        ("a confirm of a part held by a broadMatch, which gives the direction",
         location("Testlib.sprocket_finite", "hasPart", 1, conjunct_relation="broadMatch",
                  step="it gives the first half", instantiation="at a sprocket",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1], DIRECTION)}, simple),
        ("a confirm of a part held by a hasPart, which gives none",
         location("Testlib.sprocket_finite", "hasPart", 1, conjunct_relation="hasPart",
                  step="it gives a piece of the first half"),
         {"action": "confirm", "clauses": ["c1"],
          "checks": checks(SIMPLE_READING, [1])}, simple),
        ("a confirm that writes the tests a record of an older rule set has none of",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget"),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a confirm that writes the instantiation such a record has none of",
         location("Testlib.Sprocket", "exactMatch", 1),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "instantiation": "at the gadget of the passage",
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a confirm of a closeMatch whose convention an older rule set allowed",
         location("Testlib.Sprocket", "closeMatch", 1, convention="junk_value",
                  note="It gives a default outside the passage's domain.",
                  instantiation="at a gadget", tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a confirm of a record whose note says what the PASSAGE does not state",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS,
                  note="The passage does not state that the gadget is finite."),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a downgrade of an exactMatch to a closeMatch",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "closeMatch", "clauses": ["c1"],
          "convention": "encoding", "note": "It indexes the widget.", "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a downgrade of an exactMatch to a broadMatch",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "broadMatch", "clauses": ["c1"],
          "tests": TESTS, "checks": checks(SIMPLE_READING, [1, 2], DIRECTION)}, simple),
        ("a downgrade of a closeMatch to a broadMatch, whose convention goes with it",
         location("Testlib.Sprocket", "closeMatch", 1, convention="encoding",
                  note="It indexes the widget.", instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "broadMatch", "clauses": ["c1"],
          "tests": TESTS, "checks": checks(SIMPLE_READING, [1, 2], DIRECTION)}, simple),
        ("a downgrade of a broadMatch to a narrowMatch",
         location("Testlib.Sprocket", "broadMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "narrowMatch", "clauses": ["c1"],
          "step": "it is the passage at a finite gadget"}, simple),
        ("a downgrade of a broadMatch to a hasPart",
         location("Testlib.Sprocket", "broadMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "hasPart", "clauses": ["c1"],
          "step": "it gives the first half alone", "conjunct_relation": "exactMatch",
          "tests": TESTS, "checks": checks(SIMPLE_READING, [1])}, simple),
        ("a downgrade to a relatedMatch with a bridge the index holds",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "relatedMatch",
          "bridge": {"library": "testlib", "declaration": "Testlib.finite_sprocket",
                     "why": "it carries the finiteness the passage asserts"}}, simple),
        ("a downgrade to a relatedMatch that says the two are incomparable",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "relatedMatch", "clauses": [],
          "incomparable": "Neither setting contains the other."}, simple),
        ("a downgrade of a hasPart to a relatedMatch",
         location("Testlib.sprocket_finite", "hasPart", 1, conjunct_relation="exactMatch",
                  step="it gives the first half", instantiation="at a sprocket",
                  tests=TESTS),
         {"action": "downgrade", "relation": "relatedMatch"}, simple),
        ("a downgrade of a narrowMatch to a relatedMatch",
         location("Testlib.Sprocket", "narrowMatch", 1, step="the gadget case alone"),
         {"action": "downgrade", "relation": "relatedMatch"}, simple),
        ("a confirm of an exactMatch on a compound statement",
         location("Testlib.Sprocket", "exactMatch", 1, clauses=both,
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "confirm", "clauses": both, "tests": TESTS,
          "checks": checks(COMPOUND_READING, [1, 2])}, compound),
        ("a confirm whose reading splits a compound statement finer than its conjuncts",
         location("Testlib.Sprocket", "exactMatch", 1, clauses=both,
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "confirm", "clauses": both, "tests": TESTS,
          "checks": checks([*COMPOUND_READING, "a finite gadget is a gadget"],
                           [1, 2, 3])}, compound),
        ("a confirm of a part of a compound statement",
         location("Testlib.sprocket_finite", "hasPart", 1, clauses=["c1"],
                  conjunct_relation="exactMatch", step="it states the first conjunct",
                  instantiation="at a sprocket", tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(COMPOUND_READING, [1])}, compound),
        ("a confirm of a part credited with both conjuncts",
         location("Testlib.sprocket_finite", "hasPart", 1, clauses=both,
                  conjunct_relation="broadMatch", step="it states both conjuncts",
                  instantiation="at a sprocket", tests=TESTS),
         {"action": "confirm", "clauses": both, "tests": TESTS,
          "checks": checks([*COMPOUND_READING, "a sprocket is a gadget"], [1, 2],
                           DIRECTION)}, compound),
        ("a downgrade of a strong relation on a compound statement to a part",
         location("Testlib.Sprocket", "exactMatch", 1, clauses=both,
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "downgrade", "relation": "hasPart", "clauses": ["c1"],
          "conjunct_relation": "closeMatch", "convention": "encoding",
          "note": "It indexes the gadget.", "step": "it states the first conjunct",
          "tests": TESTS, "checks": checks(COMPOUND_READING, [1])}, compound),
        ("a downgrade of a part of a compound statement to a narrowMatch",
         location("Testlib.sprocket_finite", "hasPart", 1, clauses=["c1"],
                  conjunct_relation="exactMatch", step="it states the first conjunct",
                  instantiation="at a sprocket", tests=TESTS),
         {"action": "downgrade", "relation": "narrowMatch", "clauses": ["c1"],
          "step": "it states the first conjunct at a finite sprocket"}, compound),
        ("a downgrade of a broadMatch on a compound statement to a closeMatch",
         location("Testlib.Sprocket", "broadMatch", 1, clauses=both,
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "downgrade", "relation": "closeMatch", "clauses": both,
          "convention": "notation", "note": "It writes the gadget the other way round.",
          "tests": TESTS, "checks": checks(COMPOUND_READING, [1, 2])}, compound),
        ("a confirm of a closeMatch on a compound statement",
         location("Testlib.Sprocket", "closeMatch", 1, clauses=both,
                  convention="bundling", note="It bundles both halves into one field.",
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "confirm", "clauses": both, "tests": TESTS,
          "checks": checks(COMPOUND_READING, [1, 2])}, compound),
        ("a confirm of a broadMatch on a compound statement",
         location("Testlib.Sprocket", "broadMatch", 1, clauses=both,
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "confirm", "clauses": both, "tests": TESTS,
          "checks": checks(COMPOUND_READING, [1, 2], DIRECTION)}, compound),
        ("a confirm of a relatedMatch on a compound statement",
         location("Testlib.Sprocket", "relatedMatch", 1, clauses=[]),
         {"action": "confirm"}, compound),
        ("a downgrade of a closeMatch to a narrowMatch, whose convention goes with it",
         location("Testlib.Sprocket", "closeMatch", 1, convention="renaming",
                  note="It renames the widget.", instantiation="at a gadget",
                  tests=TESTS),
         {"action": "downgrade", "relation": "narrowMatch", "clauses": ["c1"],
          "step": "it is the passage at a finite gadget"}, simple),
        ("a confirm of a part held by a narrowMatch",
         location("Testlib.sprocket_finite", "hasPart", 1, clauses=["c1"],
                  conjunct_relation="narrowMatch", step="it states a case of c1"),
         {"action": "confirm", "clauses": ["c1"],
          "checks": checks(COMPOUND_READING, [1])}, compound),
        ("a confirm of a part that carries the pd6 line of a bundle",
         location("Testlib.sprocket_finite", "hasPart", 1, clauses=["c1"],
                  conjunct_relation="exactMatch", step="it states the first conjunct",
                  pd6="It concludes with an iff and does not state c2 on its own.",
                  instantiation="at a sprocket", tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(COMPOUND_READING, [1])}, compound),
        ("a downgrade that writes its own note over the record's",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  note="It is the gadget itself.", tests=TESTS),
         {"action": "downgrade", "relation": "broadMatch", "clauses": ["c1"],
          "note": "It is the gadget over any carrier.", "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2], DIRECTION)}, simple),
        ("a confirm of a record whose note says what ANOTHER declaration does not state",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS,
                  note="Unlike the dual, which does not state the widget, it states it."),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2])}, simple),
        ("a confirm whose clauses name the compound's conjuncts in another order",
         location("Testlib.Sprocket", "exactMatch", 1, clauses=both,
                  instantiation="at the sprockets", tests=TESTS),
         {"action": "confirm", "clauses": ["c2", "c1"], "tests": TESTS,
          "checks": checks(COMPOUND_READING, [2, 1])}, compound),
        ("a confirm of a notation reading the kind check spells out",
         location("Testlib.Sprocket", "exactMatch", 1, instantiation="at a gadget",
                  tests=TESTS),
         {"action": "confirm", "clauses": ["c1"], "tests": TESTS,
          "checks": checks(SIMPLE_READING, [1, 2], None,
                           "a notation passage, and the structure carries the "
                           "notation itself")}, simple),
    ]


class LegitimateVerdictsTests(unittest.TestCase):
    """Thirty-odd verdicts a challenger may legally write, none of them refused.

    Every refusal the gate adds costs a model call on every run that trips it, so the
    rules of issue #175 are measured against a battery of shapes that must pass: every
    relation, both actions that keep a record, a record whose fields the verdict
    replaces and one whose fields it inherits, and records written under older rule
    sets. Synthetic, like the rest of this file.
    """

    def test_no_legitimate_verdict_is_refused(self):
        battery = legitimate_verdicts()
        self.assertGreaterEqual(len(battery), 30)
        records, verdicts = [], []
        for number, (what, loc, verdict, passage) in enumerate(battery):
            key = f"{number:064x}"
            records.append(_challenge_record(passage, key, loc))
            verdicts.append({"item": passage["item"], "target": "location",
                             "location_key": key, "witness": f"It is {what}.",
                             **verdict})
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp), mode=vocab.MODE_CHALLENGE, judgment_rules=5)
            fixture.set_records(records)
            document = good_challenge_document()
            document["verdicts"] = verdicts
            fixture.write(document)
            code, text = fixture.run()
            self.assertEqual(code, gate.EXIT_OK, text)

    def test_each_one_passes_on_its_own(self):
        """One document per verdict, so a refusal names which shape it refused."""
        for number, (what, loc, verdict, passage) in enumerate(legitimate_verdicts()):
            with self.subTest(what=what):
                key = f"{number:064x}"
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = Fixture(Path(tmp), mode=vocab.MODE_CHALLENGE,
                                      judgment_rules=5)
                    fixture.set_records([_challenge_record(passage, key, loc)])
                    document = good_challenge_document()
                    document["verdicts"] = [
                        {"item": passage["item"], "target": "location",
                         "location_key": key, "witness": f"It is {what}.", **verdict}]
                    fixture.write(document)
                    code, text = fixture.run()
                    self.assertEqual(code, gate.EXIT_OK, f"{what}:\n{text}")


def _sentence(text: str, formulas: tuple = (), block: str = "p") -> lt.Blocks:
    """One synthetic paragraph as a :class:`locate_text.Blocks`.

    Every sentence passed here is invented for this file. Issue #172 names the article
    sentences that prompted the check; none of them is in this repository.
    """
    paragraphs = [{"block": block, "span": [0, len(text)], "math": block == "math"}]
    return lt.Blocks(text, paragraphs,
                     [{"span": list(span), "alttext": alt} for span, alt in formulas])


class SentenceCutTests(unittest.TestCase):
    """Statement-cut rule 4: which starts inside a sentence are cuts, and which are not.

    One case per cut issue #172 lists, and one per cut the inventory prompt allows
    there. ``cuts_a_sentence`` is what gate rule 10 and ``audit_sentences.py`` both call.
    """

    #: (what it is, the sentence, the words the passage would start at) — refused.
    REFUSED = (
        ("a bare \"and\" inside a noun phrase",
         "The empty set and the whole space are both open.", "the whole space"),
        ("a list comma",
         "Widgets, gadgets and sprockets are all finite.", "gadgets and"),
        ("a hypothesis and its conclusion",
         "If x and y are widgets, then the product xy is a widget.", "then the product"),
        ("inside a noun phrase joined by \"or\"",
         "Open subsets of a widget or a gadget are connected.", "a gadget are"),
        ("a bare \"and\" joining two equations",
         "Here 4 = 2 x 2 and 6 = 2 x 3 are both composite.", "6 = 2 x 3"),
        ("two conclusions one hypothesis governs",
         "If W is finite, then W is round, and it is sprocketed.", "it is sprocketed"),
    )
    #: The same, for the cuts the prompt does put inside a sentence — all allowed.
    ALLOWED = (
        ("\", and\" before a clause of its own",
         "A widget is finite, and it is round.", "it is round"),
        ("a semicolon",
         "A widget is round; it is also finite.", "it is also"),
        ("the connective \"so\"",
         "A widget is round, so it is finite.", "it is finite"),
        ("the connective \"hence\"",
         "A widget is finite, hence it is round.", "it is round"),
        ("the connective \"moreover\"",
         "A widget is round, moreover it is finite.", "it is finite"),
        ("the conclusion after the reason its sentence opens with",
         "Since a widget is finite, it is round.", "it is round"),
        ("an inline list item",
         "The cases are: a) finite widgets, b) round widgets.", "b) round"),
        ("a bracketed inline list item",
         "The cases are: (a) finite widgets, (b) round widgets.", "(b) round"),
        ("a numbered inline list item",
         "The cases are: 1) finite widgets, 2) round widgets.", "2) round"),
        ("a bracketed numbered inline list item",
         "The cases are: (1) finite widgets, (2) round widgets.", "(2) round"),
        ("a connective written with a comma after it",
         "A widget is round; moreover, it is finite.", "it is finite"),
        ("\"hence,\" with a comma after it",
         "A widget is finite, hence, it is round.", "it is round"),
        ("\"so,\" with a comma after it",
         "A widget is round, so, it is finite.", "it is finite"),
    )

    def test_a_cut_no_rule_allows_is_a_fault(self):
        for what, text, words in self.REFUSED:
            with self.subTest(what=what):
                blocks = _sentence(text)
                start = text.index(words)
                self.assertTrue(blocks.mid_sentence(start), what)
                self.assertIsNone(blocks.allowed_cut(start), what)
                self.assertTrue(blocks.cuts_a_sentence(start), what)

    def test_every_cut_the_prompt_allows_stays_allowed(self):
        for what, text, words in self.ALLOWED:
            with self.subTest(what=what):
                blocks = _sentence(text)
                start = text.index(words)
                self.assertTrue(blocks.mid_sentence(start), what)
                self.assertIsNotNone(blocks.allowed_cut(start), what)
                self.assertFalse(blocks.cuts_a_sentence(start), what)

    def test_a_second_display_formula_starts_a_passage(self):
        text = "S = W T = V"
        blocks = _sentence(text, formulas=(([0, 5], "S = W"), ([6, 11], "T = V")),
                           block="math")
        self.assertTrue(blocks.mid_sentence(6))
        self.assertEqual(blocks.allowed_cut(6), lt.CUT_FORMULA)
        self.assertFalse(blocks.cuts_a_sentence(6))

    def test_a_formula_inside_a_paragraph_is_no_display_and_starts_nothing(self):
        text = "Note S = W and T = V here."
        blocks = _sentence(text, formulas=(([5, 10], "S = W"), ([15, 20], "T = V")))
        self.assertTrue(blocks.cuts_a_sentence(15))

    def test_a_passage_that_starts_a_sentence_is_no_cut_at_all(self):
        text = "A widget is round. A gadget is finite."
        blocks = _sentence(text)
        for start in (0, text.index("A gadget")):
            self.assertFalse(blocks.mid_sentence(start))
            self.assertFalse(blocks.cuts_a_sentence(start))

    #: A cited sentence, and the sentence after it: the reference superscripts a
    #: Wikipedia article writes between them are no characters of either.
    CITED = (
        ("one mark", "A widget is round.[3] A gadget is finite.", "A gadget"),
        ("a two-digit mark", "A widget is round.[34] A gadget is finite.", "A gadget"),
        ("two marks", "A widget is round.[3][4] A gadget is finite.", "A gadget"),
        ("a lettered mark", "A widget is round.[a] A gadget is finite.", "A gadget"),
        ("a note mark", "A widget is round.[nb 1] A gadget is finite.", "A gadget"),
    )

    def test_a_footnote_mark_does_not_hide_the_sentence_it_follows(self):
        """A passage that starts the sentence after a cited one starts no sentence.

        The gate refuses a re-cut it would cost a model call to make and the worker
        could not legally make, so a mark left in the reading is a false refusal on
        every article that cites anything.
        """
        for what, text, words in self.CITED:
            with self.subTest(what=what):
                blocks = _sentence(text)
                start = text.index(words)
                self.assertFalse(blocks.mid_sentence(start), what)
                self.assertFalse(blocks.cuts_a_sentence(start), what)
                self.assertEqual(blocks.sentence_at(start), (start, len(text)), what)

    def test_a_mark_inside_a_sentence_still_leaves_the_cut_inside_it(self):
        """The mark is set aside, the words around it are not: a bare "and" is still one."""
        text = "The empty set[3] and the whole space are both open."
        blocks = _sentence(text)
        self.assertTrue(blocks.cuts_a_sentence(text.index("the whole space")))

    def test_a_cited_sentence_does_not_carry_its_hypothesis_into_the_next(self):
        """The hypothesis veto is the opener of the start's own sentence, not of a
        sentence a mark glued to it."""
        text = "If W is finite, then W is round.[3] A gadget is finite, so it is round."
        blocks = _sentence(text)
        start = text.index("it is round.", text.index("[3]"))
        self.assertEqual(blocks.allowed_cut(start), "the connective 'so'")
        self.assertFalse(blocks.cuts_a_sentence(start))

    def test_a_reason_after_a_cited_sentence_is_still_a_reason(self):
        text = "A widget is round.[3] Since W is finite, W is sprocketed."
        blocks = _sentence(text)
        start = text.index("W is sprocketed")
        self.assertEqual(blocks.allowed_cut(start), lt.CUT_REASON)
        self.assertFalse(blocks.cuts_a_sentence(start))

    def test_every_passage_of_the_synthetic_source_is_a_whole_sentence(self):
        blocks = lt.Blocks(LAYER, PARAGRAPHS, FORMULAS)
        for passage in INVENTORY_PASSAGES:
            self.assertFalse(blocks.cuts_a_sentence(passage["span"][0]), passage["item"])
        self.assertTrue(blocks.cuts_a_sentence(BARE_AND))


class AuditTests(unittest.TestCase):
    """``audit_sentences.py``: the gate's check over a sealed set, reading only.

    The set is the pipeline's own synthetic fixture, written here with one article's
    sentence cut at a bare "and", so the audit has something to count. Nothing in this
    test touches the committed fixture.
    """

    @classmethod
    def setUpClass(cls):
        fixtures = str(HERE.parent / "fixtures")      # make_fixture, the set's writer
        if fixtures not in sys.path:
            sys.path.insert(0, fixtures)

    def _set(self, root: Path) -> tuple[Path, Path]:
        """``(sealed set, rendered sources)``, the set's second passage cut mid-sentence."""
        import make_fixture

        layers = dict(make_fixture.LAYERS)
        layers["enwiki-1001-2002"] = [
            "A widget is a gadget that carries a handle and a spanning frame around it.",
            "Every finite widget admits a spanning gadget and",
            "two such gadgets of one finite widget are equal.",
            "Worked example: the three-handled widget has one spanning gadget, drawn below.",
            "A reader who prefers frames to handles may skip the rest of this section.",
        ]
        with mock.patch.object(make_fixture, "LAYERS", layers):
            make_fixture.write(root / "set")
            sources = root / "sources"
            for source_id in layers:
                layer, _spans = make_fixture.layer_of(source_id)
                directory = sources / source_id
                directory.mkdir(parents=True)
                (directory / "layer.txt").write_text(layer, encoding="utf-8")
                (directory / "paragraphs.json").write_text(
                    json.dumps([{"block": "p", "span": [0, len(layer)], "math": False}]),
                    encoding="utf-8")
                (directory / "formulas.json").write_text("[]", encoding="utf-8")
        return root / "set", sources

    def test_the_audit_counts_the_fragments_and_names_what_they_truncate(self):
        import audit_sentences
        import make_fixture

        with tempfile.TemporaryDirectory() as tmp:
            set_dir, sources = self._set(Path(tmp))
            result = audit_sentences.audit(set_dir, sources)
            self.assertEqual(result["totals"]["passages"], 1)
            self.assertEqual(result["totals"]["located"], 1)
            self.assertEqual(result["totals"]["sources_checked"], 2)
            self.assertEqual(result["totals"]["sources_with_a_cut_no_rule_allows"], 1)
            entry = result["sources"][0]["passages"][0]
            self.assertEqual(len(entry["passage_key"]), 64)
            self.assertIsNotNone(entry["truncates"])
            # it prints keys, ids, offsets and counts — never the article's own words
            printed = "\n".join(audit_sentences.report(result))
            for source_id in ("enwiki-1001-2002", "enwiki-1003-2004"):
                words = make_fixture.layer_of(source_id)[0].split()
                for at in range(len(words) - 2):
                    self.assertNotIn(" ".join(words[at:at + 3]), printed)

    def test_the_audit_says_which_sources_it_could_not_check(self):
        import audit_sentences

        with tempfile.TemporaryDirectory() as tmp:
            set_dir, sources = self._set(Path(tmp))
            (sources / "enwiki-1003-2004" / "layer.txt").write_text("not the layer",
                                                                    encoding="utf-8")
            result = audit_sentences.audit(set_dir, sources)
            self.assertEqual([row["source_id"] for row in result["unchecked"]],
                             ["enwiki-1003-2004"])
            self.assertIn("a span is an offset into one layer",
                          result["unchecked"][0]["reason"])
            self.assertEqual(result["totals"]["sources_checked"], 1)

    def test_the_exit_code_says_whether_anything_was_found(self):
        import audit_sentences

        with tempfile.TemporaryDirectory() as tmp:
            set_dir, sources = self._set(Path(tmp))
            written = io.StringIO()
            with redirect_stdout(written), redirect_stderr(io.StringIO()):
                found = audit_sentences.main(["--record-set", str(set_dir),
                                              "--sources", str(sources), "--json"])
            self.assertEqual(found, audit_sentences.EXIT_FOUND)
            self.assertEqual(json.loads(written.getvalue())["totals"]["passages"], 1)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                missing = audit_sentences.main(["--record-set", str(set_dir),
                                                "--sources", str(Path(tmp) / "none")])
            self.assertEqual(missing, audit_sentences.EXIT_REFUSED)


class StagingTests(unittest.TestCase):
    """The gate runs out of tree, and refuses to be staged inside the checkout."""

    def test_a_staged_directory_runs_the_gate(self):
        import stage as stage_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = Fixture(root / "work")
            out = root / "gate"
            written = stage_mod.stage(out, fixture.gate_path,
                                      [fixture.root / "tasks" / f"{TASK}.json"])
            for name, _sha in written.items():
                self.assertTrue((out / name).is_file(), name)
            self.assertIn("anchor_resolve.py", written)
            # and staging it twice gives the same bytes
            self.assertEqual(written, stage_mod.stage(out, fixture.gate_path,
                                                      [fixture.root / "tasks"
                                                       / f"{TASK}.json"]))

    def test_staging_inside_the_checkout_is_refused(self):
        import stage as stage_mod

        with self.assertRaises(stage_mod.StageProblem):
            stage_mod.check_target(stage_mod.ROOT / "scripts" / "pipeline" / "scratch-gate")


class EveryRuleIsCoveredTests(unittest.TestCase):
    def test_a_test_exists_for_each_of_the_ten_rules(self):
        names = [name for name in dir(EachRuleFiresTests) if name.startswith("test_rule")]
        covered = {int(name.split("_")[1][len("rule"):]) for name in names}
        self.assertEqual(covered, set(range(1, 11)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
