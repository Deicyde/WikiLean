#!/usr/bin/env python3
"""Stages 9b, 9c and 9d: the cut, the retrieval, the locate plan and the challenge plan.

docs/PIPELINE.md sections 3 and 8. The synthetic source is the gate suite's
(``scripts/pipeline/gate/test_gate.py``): one fixture, three suites, so a document that
passes here is the document the gate gates.

**No model is called and none could be.** Every worker in this file is a local command
that writes a recorded document, run through the same ``run.agent`` entry a model call
takes; the real call needs ``--execute``. The end-to-end test drives one synthetic
article from a sourceset to a challenge plan with nothing paid for.

The batch-1 proofs — the challenge plan of ``chal-tierA-b1`` reproduced from batch 1's
own pre-challenge outcome rows, all 13,079 records and all 351 task manifests — are
operator runs over data this repository does not hold, and their results are in the
report.

Run: python3 scripts/pipeline/test_model_stages.py
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE.parents[1] / "site", HERE / "run", HERE / "gate", HERE):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import agent as agent_mod  # noqa: E402
import candidates as candidates_mod  # noqa: E402
import challenge as challenge_mod  # noqa: E402
import cut as cut_mod  # noqa: E402
import export_records  # noqa: E402
import export_statements  # noqa: E402
import harvest as harvest_mod  # noqa: E402
import locate as locate_mod  # noqa: E402
import records as records_mod  # noqa: E402
import statements as statements_mod  # noqa: E402
import plan as plan_mod  # noqa: E402
import prompts as prompts_mod  # noqa: E402
import sourceset as sourceset_mod  # noqa: E402
import tally as tally_mod  # noqa: E402
import test_gate as fixture  # noqa: E402 — scripts/pipeline/gate/test_gate.py
import verify_locate as gate  # noqa: E402
import vocab  # noqa: E402

REVISION = 1000000007
UNIVERSE = {
    "id": "u-test", "universe_sha256": fixture.UNIVERSE_SHA, "frozen": "2026-01-01",
    "libraries": [{"id": "testlib", "brain_lib": "testlib",
                   "repo": "https://example.invalid/testlib", "commit": fixture.COMMIT,
                   "toolchain": "leanprover/lean4:v4.0.0", "roots": ["Testlib"],
                   "licence": {"spdx": "Apache-2.0"}}],
    "excluded": [],
}


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def entry(**over) -> dict:
    row = {"assessment": {"class": "C", "importance": "low"}, "family": "enwiki",
           "fetch": None, "flags": {"biography": False, "disambiguation": False,
                                    "list": False, "redirect": False},
           "html_sha256": fixture.HTML_SHA,
           "layer_sha256": gate.sha256_bytes(fixture.LAYER.encode("utf-8")),
           "licence_class": "open_share_alike", "origin": "adopted",
           "pageid": fixture.PAGEID, "raw_sha256": None,
           "renderer": vocab.RENDERER_WEB_HTML, "revision": REVISION,
           "source_id": fixture.SOURCE, "title": "Sprocket", "wikidata": "Q1"}
    row.update(over)
    return row


class Workspace:
    """A synthetic sourceset with one rendered source, a universe and an index."""

    def __init__(self, root: Path, *, extra_entries=()):
        self.root = Path(root)
        self.ss = sourceset_mod.Sourceset.create(self.root / "wpm-test", "wpm-test",
                                                 "enwiki", created="2026-01-01T00:00:00Z")
        entries = {fixture.SOURCE: entry()}
        for row in extra_entries:
            entries[row["source_id"]] = row
        self.ss.write_version(entries, created="2026-01-02T00:00:00Z")
        directory = self.ss.source_dir(fixture.SOURCE)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "layer.txt").write_text(fixture.LAYER, encoding="utf-8")
        (directory / "text.txt").write_text(fixture.LAYER, encoding="utf-8")
        _write(directory / "sections.json", [])
        _write(directory / "paragraphs.json", fixture.PARAGRAPHS)
        _write(directory / "formulas.json", fixture.FORMULAS)
        _write(directory / "finders.json", fixture.FINDERS)
        # the synthetic reading copy IS the layer, so its map is the identity
        _write(directory / "layer_map.json",
               {"schema": "cf-layer-map/2", "renderer": vocab.RENDERER_WEB_HTML,
                "segments": [[0, len(fixture.LAYER), 0, len(fixture.LAYER), 0]]})
        self.universe = self.root / "universe.json"
        _write(self.universe, UNIVERSE)
        self.index = self.root / "decls.jsonl"
        self.index.write_text(
            json.dumps({"universe_sha256": fixture.UNIVERSE_SHA}) + "\n"
            + "".join(json.dumps({**row, "wikidata": ("Q1" if row["declaration"]
                                                      == fixture.DECLS[0]["declaration"]
                                                      else None),
                                  "module": "Testlib.Basic"}) + "\n"
                      for row in fixture.DECLS), encoding="utf-8")
        self.candidates = self.root / "candidates"

    def inventory_corpus(self) -> Path:
        """A harvested inventory corpus holding the fixture's inventory document."""
        corpus = self.root / "corpus"
        inv = corpus / fixture.INV_RUN / fixture.INV_TASK
        _write(inv / f"{fixture.SOURCE}.json", fixture.good_inventory_document())
        _write(corpus / fixture.INV_RUN / "RECEIPT.json",
               {"complete": True, "gate": {"ok": True},
                "harvested_at": "2026-01-02T03:04:05Z"})
        return corpus / fixture.INV_RUN


class CutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root / "ws")

    def plan(self, out: Path, **kw) -> dict:
        return cut_mod.plan_inventory(run_id=fixture.INV_RUN, out=out,
                                      sourceset_dir=self.ws.ss.dir,
                                      universe=self.ws.universe, **kw)

    def test_one_task_per_source_with_the_finders_pre_filled(self):
        planned = self.plan(self.root / "inv")
        self.assertEqual(len(planned["tasks"]), 1)
        task = planned["tasks"][0]
        self.assertEqual(task["task_id"], f"inv-{fixture.SOURCE}")
        self.assertEqual(task["mode"], vocab.MODE_INVENTORY)
        document = task["documents"][0]
        self.assertEqual(document["source_id"], fixture.SOURCE)
        self.assertEqual(document["finders"], fixture.FINDERS)
        self.assertEqual(document["layer_sha256"],
                         gate.sha256_bytes(fixture.LAYER.encode("utf-8")))

    def test_a_source_with_no_layer_is_skipped_by_name_not_planned(self):
        ws = Workspace(self.root / "ws2", extra_entries=[
            entry(source_id="enwiki-5-6", pageid=5, revision=6, html_sha256=None,
                  layer_sha256=None, renderer=None, title="Unrendered")])
        planned = cut_mod.plan_inventory(run_id=fixture.INV_RUN, out=self.root / "inv2",
                                         sourceset_dir=ws.ss.dir, universe=ws.universe)
        self.assertEqual(len(planned["tasks"]), 1)
        self.assertEqual(planned["skipped"],
                         [{"source_id": "enwiki-5-6", "reason": vocab.SKIP_NOT_RENDERED}])

    def test_a_page_that_is_no_concept_article_is_skipped(self):
        flagged = entry(source_id="enwiki-5-6", pageid=5, revision=6, title="List of x",
                        flags={"biography": False, "disambiguation": False, "list": True,
                               "redirect": False})
        ws = Workspace(self.root / "ws3", extra_entries=[flagged])
        planned = cut_mod.plan_inventory(run_id=fixture.INV_RUN, out=self.root / "inv3",
                                         sourceset_dir=ws.ss.dir, universe=ws.universe)
        self.assertEqual(planned["skipped"][0]["reason"],
                         f"{vocab.SKIP_NO_CONCEPT} (list)")

    def test_a_source_with_no_finders_file_is_named(self):
        (self.ws.ss.source_dir(fixture.SOURCE) / "finders.json").unlink()
        with self.assertRaises(cut_mod.CutProblem) as caught:
            self.plan(self.root / "inv")
        self.assertIn("finders.json", str(caught.exception))

    def test_a_replan_of_an_unchanged_cut_writes_the_same_bytes(self):
        out, other = self.root / "a", self.root / "b"
        self.plan(out)
        self.plan(other)
        self.assertEqual((out / "planned.json").read_bytes(),
                         (other / "planned.json").read_bytes())
        self.assertEqual((out / "tasks" / f"inv-{fixture.SOURCE}.json").read_bytes(),
                         (other / "tasks" / f"inv-{fixture.SOURCE}.json").read_bytes())

    def test_the_prompts_are_rendered_into_the_root_and_recorded_by_sha(self):
        out = self.root / "inv"
        self.plan(out, render_prompts=True)
        config = json.loads((out / "run.json").read_text())
        self.assertEqual(sorted(config["prompts"]), sorted(prompts_mod.PROMPT_FILES))
        text = (out / "prompts" / "worker.md").read_text(encoding="utf-8")
        self.assertIn("inventory", text)
        self.assertNotIn("{{", text)

    def test_the_frozen_inventory_reads_back_the_quote_and_the_reading_copy_span(self):
        corpus = self.ws.inventory_corpus()
        index = cut_mod.passage_index(corpus)
        self.assertTrue(index)
        for (sid, item), passage in index.items():
            self.assertEqual(sid, fixture.SOURCE)
            self.assertEqual(passage["item"], item)
        counts = cut_mod.counts(cut_mod.frozen_inventory(corpus))
        self.assertEqual(counts["sources"], 1)
        self.assertEqual(counts["located"],
                         sum(1 for p in fixture.good_inventory_document()["passages"]
                             if p["scope"] == vocab.SCOPE_FORMAL_MATH))


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root / "ws")

    def run_retrieval(self, **kw) -> dict:
        shas = candidates_mod.run(sourceset_dir=self.ws.ss.dir, index=self.ws.index,
                                  universe=self.ws.universe, out=self.ws.candidates, **kw)
        document = json.loads(
            (self.ws.candidates / f"{fixture.SOURCE}.json").read_text(encoding="utf-8"))
        self.shas = shas
        return document

    def test_a_curated_hit_is_the_wikidata_tag_and_nothing_else(self):
        document = self.run_retrieval()
        self.assertEqual([hit["declaration"] for hit in document["curated"]],
                         [fixture.DECLS[0]["declaration"]])
        self.assertEqual(document["curated"][0]["via"], candidates_mod.VIA_WIKIDATA_TAG)
        self.assertEqual(document["curated"][0]["evidence"], "@[wikidata Q1]")
        self.assertEqual(document["article_qid"], "Q1")

    def test_an_article_with_no_wikidata_item_has_no_curated_hit(self):
        retrieval = candidates_mod.Retrieval.load(self.ws.index)
        document = candidates_mod.build(
            entry(wikidata=None), retrieval, self.ws.ss.dir / "sources",
            universe="u-test", universe_sha256=fixture.UNIVERSE_SHA, index_sha256="x")
        self.assertEqual(document["curated"], [])
        self.assertIsNone(document["article_qid"])
        self.assertTrue(document["lexical"])       # the lexical search still runs

    def test_the_lexical_hits_are_deterministic_and_capped(self):
        first = self.run_retrieval(limit=2)
        second = self.run_retrieval(limit=2)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first["lexical"]), 2)
        scores = [hit["score"] for hit in first["lexical"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_term_that_is_a_label_a_symbol_or_a_stop_word_is_not_queried(self):
        for bad in ("Proof.", "R", "the", "of the", "", None):
            self.assertFalse(candidates_mod.is_lexical_term(bad), repr(bad))
        for good in ("Sprocket", "spanning gadget", "widget space"):
            self.assertTrue(candidates_mod.is_lexical_term(good), good)

    def test_a_declaration_is_tokenised_by_dots_underscores_and_case(self):
        self.assertEqual(candidates_mod.declaration_tokens("Widget.exists_spanGadget"),
                         frozenset({"widget", "exists", "span", "gadget"}))
        self.assertIn("topology", candidates_mod.declaration_tokens(
            "Foo.bar", "Mathlib.Topology.Basic"))

    def test_the_score_is_the_overlap_and_zero_when_nothing_matches(self):
        self.assertEqual(candidates_mod.score(frozenset("ab"), frozenset("ab")), 1.0)
        self.assertEqual(candidates_mod.score(frozenset("a"), frozenset("b")), 0.0)
        self.assertEqual(candidates_mod.score(frozenset(), frozenset("a")), 0.0)

    def test_an_index_built_for_another_universe_is_refused(self):
        other = self.root / "other.jsonl"
        other.write_text(json.dumps({"universe_sha256": "f" * 64}) + "\n",
                         encoding="utf-8")
        with self.assertRaises(candidates_mod.CandidateProblem) as caught:
            candidates_mod.run(sourceset_dir=self.ws.ss.dir, index=other,
                               universe=self.ws.universe, out=self.ws.candidates)
        self.assertIn("universe", str(caught.exception))

    def test_the_file_sha_is_what_a_plan_pins(self):
        self.run_retrieval()
        import hashlib  # noqa: PLC0415

        path = self.ws.candidates / f"{fixture.SOURCE}.json"
        self.assertEqual(self.shas[fixture.SOURCE],
                         hashlib.sha256(path.read_bytes()).hexdigest())


class ChallengePlanTests(unittest.TestCase):
    """Which records a challenge judges — on rows written here, not on batch 1's."""

    def row(self, item: str, **over) -> dict:
        base = {"source_id": fixture.SOURCE, "item": item, "prominence": "supporting",
                "scope": vocab.SCOPE_FORMAL_MATH, "passage_kind": "theorem",
                "outcome": vocab.OUTCOME_LOCATED, "span": [0, 10], "section": [],
                "status": "formalized", "locations": [], "coverage": None}
        base.update(over)
        return base

    def location(self, key: str, relation: str, rank: int, **over) -> dict:
        loc = {"location_key": key, "library": "testlib", "declaration": f"d{rank}",
               "relation": relation, "rank": rank, "proof_status": "proved",
               "statement": False, "note": ""}
        loc.update(over)
        return loc

    def test_every_location_of_a_central_passage_is_refuted_by_default(self):
        row = self.row("In-text #00000001", prominence="central",
                       locations=[self.location("a" * 64, "exactMatch", 1)])
        records = challenge_mod.classify([row], run_id="r1")
        by = {rec["target"]: rec for rec in records}
        self.assertEqual(by["location"]["rule"], challenge_mod.REFUTE)
        self.assertNotIn("prominence", by)      # only supporting and subject get one

    def test_an_absence_on_a_central_passage_is_hunted(self):
        row = self.row("In-text #00000002", prominence="central",
                       outcome=vocab.OUTCOME_ABSENT, status=None)
        records = challenge_mod.classify([row], run_id="r1")
        self.assertEqual([r["rule"] for r in records if r["target"] == "absence"],
                         [challenge_mod.HUNT])

    def test_a_subject_prominence_is_required_whatever_the_rates(self):
        row = self.row("In-text #00000003", prominence="subject")
        records = challenge_mod.draw(challenge_mod.classify([row], run_id="r1"),
                                     seed=0, sample_rate=0.0, strong_sample_rate=0.0,
                                     absence_sample_rate=0.0)
        prom = [r for r in records if r["target"] == "prominence"][0]
        self.assertEqual(prom["rule"], challenge_mod.REFUTE)
        self.assertEqual(prom["stratum"], challenge_mod.SUBJECT_STRATUM)
        self.assertEqual(prom["planned"], vocab.CHALLENGE_REQUIRED)

    def test_a_coverage_claim_is_refuted_by_default_whatever_the_prominence(self):
        row = self.row("In-text #00000004",
                       locations=[self.location("a" * 64, "hasPart", 1),
                                  self.location("b" * 64, "hasPart", 2)],
                       coverage={"complete": True, "parts": ["a" * 64, "b" * 64],
                                 "note": "both"})
        records = challenge_mod.classify([row], run_id="r1")
        claim = [r for r in records if r["target"] == vocab.COVERAGE_TARGET][0]
        self.assertEqual(claim["rule"], challenge_mod.REFUTE)
        self.assertEqual([p["location_key"] for p in claim["parts"]],
                         ["a" * 64, "b" * 64])

    def test_a_strong_note_that_admits_a_missing_clause_is_refuted_by_default(self):
        row = self.row("In-text #00000005", locations=[
            self.location("a" * 64, "exactMatch", 1,
                          note="The declaration does not state the uniqueness clause.")])
        records = challenge_mod.classify([row], run_id="r1")
        self.assertEqual([r["rule"] for r in records if r["target"] == "location"],
                         [challenge_mod.REFUTE])

    def test_a_basis_of_a_supporting_passage_is_required(self):
        row = self.row("In-text #00000006", status="formalized", locations=[
            self.location("a" * 64, "exactMatch", 1)])
        records = challenge_mod.classify([row], run_id="r1",
                                         basis_rule=vocab.STATUS_RULE_BROAD)
        self.assertEqual([r["rule"] for r in records if r["target"] == "location"],
                         [challenge_mod.BASIS])
        # a passage with no status has no basis
        row = self.row("In-text #00000007", status=None, locations=[
            self.location("a" * 64, "exactMatch", 1)])
        self.assertEqual(challenge_mod.bases(row, vocab.STATUS_RULE_BROAD), [])

    def test_a_weak_basis_is_required_under_plan_rule_two(self):
        """Issue #175, pattern 4: a hasPart that alone makes a passage partial sets its
        status as surely as a strong location that alone makes it formalized."""
        def row():
            return self.row("In-text #0000000a", status="partial", locations=[
                self.location("a" * 64, "hasPart", 1)])

        old = challenge_mod.classify([row()], run_id="r1")
        self.assertEqual([(r["rule"], r["stratum"]) for r in old
                          if r["target"] == "location"],
                         [(challenge_mod.SAMPLE, "relation:hasPart")])
        new = challenge_mod.classify(
            [row()], run_id="r1",
            plan_rule=vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS)
        self.assertEqual([r["rule"] for r in new if r["target"] == "location"],
                         [challenge_mod.BASIS])

    def test_a_haspart_on_a_passage_with_no_conjuncts_is_refuted_by_default(self):
        """R4 gives a part to a passage that says several things (issue #175)."""
        def classify(plan_rule, status_rule, **over):
            row = self.row("In-text #0000000c", status=None,
                           locations=[self.location("a" * 64, "hasPart", 1)], **over)
            return [r["rule"] for r in challenge_mod.classify(
                [row], run_id="r1", plan_rule=plan_rule, status_rule=status_rule)
                if r["target"] == "location"]

        simple = {"statement": "A widget is round."}
        compound = {"statement": "A widget is round and finite.",
                    "shape": "conclusions",
                    "conjuncts": [{"id": "c1", "text": "a widget is round"},
                                  {"id": "c2", "text": "a widget is finite"}]}
        self.assertEqual(classify(vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS,
                                  vocab.STATUS_RULE_BROAD, **simple),
                         [challenge_mod.REFUTE])
        # a part of a compound statement is what R4 is for, and is sampled as before
        self.assertEqual(classify(vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS,
                                  vocab.STATUS_RULE_BROAD, **compound),
                         [challenge_mod.SAMPLE])
        # and the rule is plan rule 2's
        self.assertEqual(classify(vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS,
                                  vocab.STATUS_RULE_BROAD, **simple),
                         [challenge_mod.SAMPLE])
        # a run that reads no bundles knows no passage's conjuncts, and this rule
        # would read every passage as simple
        self.assertEqual(classify(vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS,
                                  vocab.STATUS_RULE_D1, **simple),
                         [challenge_mod.SAMPLE])

    def test_a_partial_link_on_a_supporting_passage_draws_at_the_strong_rate(self):
        rates = challenge_mod.rates_of(sample_rate=0.2, strong_sample_rate=0.5,
                                       absence_sample_rate=0.5)
        for relation in ("hasPart", "narrowMatch"):
            with self.subTest(relation=relation):
                stratum = f"relation:{relation}"
                self.assertEqual(challenge_mod.stratum_rate(
                    stratum, rates, vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS), 0.2)
                self.assertEqual(challenge_mod.stratum_rate(
                    stratum, rates, vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS), 0.5)
        # a relatedMatch is no partial credit, and keeps the plan's own rate
        self.assertEqual(challenge_mod.stratum_rate(
            "relation:relatedMatch", rates, vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS), 0.2)

    def test_the_draw_takes_half_the_partial_links_under_plan_rule_two(self):
        def rows():
            # no status, so none of them is a basis, and a compound statement, so none
            # is refuted by default either: the stratum is the whole question
            return [self.row(f"In-text #{n:08x}", status=None, shape="conclusions",
                             conjuncts=[{"id": "c1", "text": "round"},
                                        {"id": "c2", "text": "finite"}],
                             locations=[self.location(f"{n:064x}", "hasPart", 1)])
                    for n in range(10)]

        for rule, wanted in ((vocab.CHALLENGE_PLAN_RULE_STRONG_BASIS, 2),
                             (vocab.CHALLENGE_PLAN_RULE_WEAK_BASIS, 5)):
            with self.subTest(plan_rule=rule):
                records = challenge_mod.classify(rows(), run_id="r1", plan_rule=rule,
                                                 status_rule=vocab.STATUS_RULE_BROAD)
                challenge_mod.draw(records, seed=0, plan_rule=rule, sample_rate=0.2,
                                   strong_sample_rate=0.5, absence_sample_rate=0.5)
                drawn = [r for r in records if r["target"] == "location"
                         and r["planned"] == vocab.CHALLENGE_REQUIRED]
                self.assertEqual(len(drawn), wanted)

    def test_the_plan_says_which_rules_it_was_drawn_under(self):
        rows = [self.row("In-text #0000000b", prominence="central",
                         locations=[self.location("a" * 64, "exactMatch", 1)])]
        for rule in vocab.CHALLENGE_PLAN_RULES:
            built = challenge_mod.plan({"loc-1": rows}, seed=0, plan_rule=rule)
            document = challenge_mod.plan_document("chal-1", built,
                                                   challenged_runs=["loc-1"])
            self.assertEqual(document["plan_rule"], rule)
        with self.assertRaises(challenge_mod.ChallengeProblem):
            challenge_mod.plan({"loc-1": rows}, seed=0, plan_rule=9)
        # and the draw itself, which a caller may reach without the plan
        with self.assertRaises(challenge_mod.ChallengeProblem):
            challenge_mod.draw(challenge_mod.classify(rows, run_id="loc-1"), seed=0,
                               plan_rule=9)

    def _main_plan(self, rows: list[dict], *args: str) -> dict:
        """``challenge-plan.json`` as the command line writes it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rows.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code = challenge_mod.main(
                    ["--rows", f"loc-1={root / 'rows.jsonl'}", "--run-id", "chal-1",
                     "--out", str(root / "out"), *args])
            self.assertEqual(code, 0)
            return json.loads((root / "out" / "challenge-plan.json").read_text())

    def test_the_command_line_reads_a_basis_under_the_runs_own_status_rule(self):
        """A broadMatch to data alone makes a theorem passage formalized under status
        rule 3, so it is that passage's basis. The command line never passed its
        ``--status-rule`` on to :func:`challenge.bases`, which read every location's own
        status under rule 1, where the same location is only ``partial`` — so the
        relation issue #175 found wrong most often was invisible to the basis rule.
        Plan rule 2 reads it under the run's rule; rule 1 keeps the old reading, so a
        replay of an older plan is unchanged.
        """
        rows = [self.row("In-text #0000000d", status="formalized", locations=[
            self.location("a" * 64, "broadMatch", 1, proof_status="definition")])]
        self.assertEqual(
            [r["rule"] for r in self._main_plan(rows, "--status-rule", "3",
                                                "--plan-rule", "2")["records"]
             if r["target"] == "location"], [challenge_mod.BASIS])
        self.assertEqual(
            [(r["rule"], r["planned"]) for r in
             self._main_plan(rows, "--status-rule", "3", "--plan-rule", "1")["records"]
             if r["target"] == "location"],
            [(challenge_mod.SAMPLE, vocab.CHALLENGE_REQUIRED)])

    def test_the_draw_is_exact_arithmetic_and_seeded(self):
        rows = [self.row(f"In-text #{n:08x}", outcome=vocab.OUTCOME_ABSENT, status=None)
                for n in range(100)]
        records = challenge_mod.classify(rows, run_id="r1")
        absences = [r for r in records if r["target"] == "absence"]
        self.assertEqual(len(absences), 100)
        challenge_mod.draw(records, seed=0, sample_rate=0.2, strong_sample_rate=0.5,
                           absence_sample_rate=0.07)
        drawn = [r for r in absences if r["planned"] == vocab.CHALLENGE_REQUIRED]
        self.assertEqual(len(drawn), 7)      # not the 8 a float product rounds to
        self.assertEqual(int(Fraction("0.07") * 100), 7)
        again = challenge_mod.classify(rows, run_id="r1")
        challenge_mod.draw(again, seed=0, sample_rate=0.2, strong_sample_rate=0.5,
                           absence_sample_rate=0.07)
        self.assertEqual({r["key"] for r in drawn},
                         {r["key"] for r in again
                          if r["target"] == "absence"
                          and r["planned"] == vocab.CHALLENGE_REQUIRED})
        other = challenge_mod.classify(rows, run_id="r1")
        challenge_mod.draw(other, seed=1, sample_rate=0.2, strong_sample_rate=0.5,
                           absence_sample_rate=0.07)
        self.assertNotEqual({r["key"] for r in drawn},
                            {r["key"] for r in other
                             if r["target"] == "absence"
                             and r["planned"] == vocab.CHALLENGE_REQUIRED})

    def test_a_non_empty_stratum_at_a_rate_above_zero_draws_at_least_one(self):
        rows = [self.row("In-text #00000001", outcome=vocab.OUTCOME_ABSENT, status=None)]
        records = challenge_mod.classify(rows, run_id="r1")
        challenge_mod.draw(records, seed=0, sample_rate=0.2, strong_sample_rate=0.5,
                           absence_sample_rate=0.01)
        self.assertEqual([r["planned"] for r in records if r["target"] == "absence"],
                         [vocab.CHALLENGE_REQUIRED])

    def test_a_rate_outside_zero_to_one_is_refused(self):
        for bad in (-0.1, 1.5, "half", True):
            with self.assertRaises(challenge_mod.ChallengeProblem):
                challenge_mod.rates_of(sample_rate=bad)

    def test_a_conditional_record_joins_its_passages_task(self):
        # an unresolved outcome on a supporting passage falls under no rule, so the
        # plan does not require it; its passage's prominence is drawn, which reopens it
        row = self.row("In-text #00000008", outcome=vocab.OUTCOME_UNRESOLVED,
                       status=None)
        records = challenge_mod.classify([row], run_id="r1")
        challenge_mod.draw(records, seed=0, sample_rate=1.0, strong_sample_rate=1.0,
                           absence_sample_rate=1.0)
        marked = challenge_mod.mark_conditional(records)
        self.assertEqual([r["target"] for r in marked], ["unresolved"])
        self.assertEqual(marked[0]["conditional"], challenge_mod.CONDITIONAL)
        groups = challenge_mod.batches(records)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)     # the prominence and its conditional

    def test_a_passage_is_never_split_over_two_tasks(self):
        rows = [self.row(f"In-text #{n:08x}", span=[n, n + 5], prominence="central",
                         locations=[self.location(f"{n:064x}", "exactMatch", 1),
                                    self.location(f"{n + 500:064x}", "hasPart", 2)])
                for n in range(10)]
        records = challenge_mod.draw(challenge_mod.classify(rows, run_id="r1"), seed=0)
        groups = challenge_mod.batches(records, size=3)
        for group in groups:
            self.assertLessEqual(len(group), 4)   # one unit may exceed the size
        seen: dict[tuple, int] = {}
        for number, group in enumerate(groups):
            for rec in group:
                key = (rec["source_id"], rec["item"])
                self.assertEqual(seen.setdefault(key, number), number)

    def test_the_plan_document_is_what_the_tally_reads_back(self):
        rows = [self.row("In-text #00000009", prominence="central",
                         locations=[self.location("a" * 64, "exactMatch", 1)])]
        built = challenge_mod.plan({"loc-1": rows}, seed=0,
                                   status_rule=vocab.STATUS_RULE_BROAD)
        document = challenge_mod.plan_document("chal-1", built,
                                               challenged_runs=["loc-1"])
        self.assertEqual(document["schema"], vocab.CHALLENGE_PLAN_SCHEMA)
        self.assertEqual(document["challenged_runs"], ["loc-1"])
        self.assertEqual(sum(document["counts"].values()), len(document["records"]))
        for record in document["records"]:
            self.assertEqual(sorted(set(challenge_mod.PLAN_ROW_KEYS) - set(record)), [])
            self.assertEqual(record["key"], tally_mod.record_key(
                record["target"], record["source_id"], record["item"],
                record["location_key"]))

    def test_a_record_that_appears_twice_is_refused(self):
        row = self.row("In-text #0000000a", prominence="central",
                       locations=[self.location("a" * 64, "exactMatch", 1)])
        with self.assertRaises(challenge_mod.ChallengeProblem):
            challenge_mod.plan({"loc-1": [row, dict(row)]}, seed=0)

    def test_a_task_manifest_leaves_the_plans_own_bookkeeping_out(self):
        rows = [self.row("In-text #0000000b", prominence="central",
                         locations=[self.location("a" * 64, "exactMatch", 1)])]
        built = challenge_mod.plan({"loc-1": rows}, seed=0)
        manifests = challenge_mod.task_documents(
            "chal-1", built, {fixture.SOURCE: {"source_sha": fixture.HTML_SHA,
                                               "layer_sha256": "b" * 64,
                                               "renderer": vocab.RENDERER_WEB_HTML}})
        self.assertEqual(len(manifests), 1)
        document = manifests[0]["documents"][0]
        self.assertEqual(document["source_sha"], fixture.HTML_SHA)
        for record in document["records"]:
            for key in challenge_mod.PLAN_ONLY_KEYS:
                self.assertNotIn(key, record)
            self.assertIn("target", record)
            self.assertIn("key", record)


class EndToEndTests(unittest.TestCase):
    """A sourceset -> the cut -> locate -> the gate -> harvest -> the tally -> 9d."""

    def _cut(self, root: Path, ws: "Workspace", corpus: Path) -> Path:
        """Stage 9b end to end: plan, a stub worker, the gate, the harvest.

        The worker is a local command writing the recorded inventory document, run
        through the same entry a model call takes. Returns the harvested corpus
        directory, which is the frozen cut every later stage reads.
        """
        inv_root = root / "inv"
        inv = cut_mod.plan_inventory(run_id=fixture.INV_RUN, out=inv_root,
                                     sourceset_dir=ws.ss.dir,
                                     universe=ws.universe, render_prompts=True)
        inv_task = inv["tasks"][0]["task_id"]
        self._stub(root, inv_root, inv_task, fixture.good_inventory_document(),
                   fixture.INV_RUN)
        worktree = inv_root / "work" / inv_task
        self.assertEqual(
            gate.main(["--gate-config", str(inv_root / "gate" / "gate.json"),
                       "--root", str(worktree),
                       "--findings-dir", str(worktree / "findings")],
                      env={"TEND_TASK_ID": inv_task}), gate.EXIT_OK)
        receipt = harvest_mod.harvest(
            run_id=fixture.INV_RUN, source=worktree / "findings",
            dest=corpus / fixture.INV_RUN, usage=inv_root / "usage",
            gate_log=inv_root / "gate-log")
        self.assertTrue(receipt["complete"], harvest_mod.describe(receipt))
        return corpus / fixture.INV_RUN

    def test_one_synthetic_article_from_the_sourceset_to_a_challenge_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws")
            corpus = root / "corpus"
            runs = root / "runs"

            # --- 9b: the cut
            self._cut(root, ws, corpus)
            inv_root = root / "inv"

            # --- 9c: the retrieval, the tasks and the prompts
            loc_root = root / "loc"
            planned = locate_mod.plan_locate(
                run_id=fixture.RUN, out=loc_root, inventory=corpus / fixture.INV_RUN,
                inventory_run=fixture.INV_RUN, sourceset_dir=ws.ss.dir,
                universe=ws.universe, index=ws.index, candidates=ws.candidates,
                retrieve=True, render_prompts=True)
            self.assertEqual(len(planned["tasks"]), 1)
            loc_task = planned["tasks"][0]["task_id"]
            curated = planned["tasks"][0]["documents"][0]["curated"]
            self.assertEqual(curated, [{"library": "testlib",
                                        "declaration": fixture.DECLS[0]["declaration"]}])
            for passage in planned["tasks"][0]["documents"][0]["passages"]:
                # the reading-copy span, mapped from the layer span by the renderer's
                # own map, is what tells the worker where to read
                self.assertEqual(passage["text_span"], passage["span"])
                self.assertNotIn("scope", passage)

            document = fixture.good_locate_document()
            handed = {p["item"] for p in planned["tasks"][0]["documents"][0]["passages"]}
            document["passages"] = [p for p in document["passages"]
                                    if p["item"] in handed]
            self._stub(root, loc_root, loc_task, document, fixture.RUN)
            worktree = loc_root / "work" / loc_task
            self.assertEqual(
                gate.main(["--gate-config", str(loc_root / "gate" / "gate.json"),
                           "--root", str(worktree),
                           "--findings-dir", str(worktree / "findings")],
                          env={"TEND_TASK_ID": loc_task}), gate.EXIT_OK)
            receipt = harvest_mod.harvest(
                run_id=fixture.RUN, source=worktree / "findings",
                dest=corpus / fixture.RUN, usage=loc_root / "usage",
                gate_log=loc_root / "gate-log",
                run_config=json.loads((loc_root / "run.json").read_text()))
            self.assertTrue(receipt["complete"], harvest_mod.describe(receipt))

            # --- 8: the tally, over the harvested documents
            for run_id, source in ((fixture.RUN, loc_root),):
                (runs / run_id).mkdir(parents=True)
                shutil.copyfile(source / "run.json", runs / run_id / "run.json")
                shutil.copyfile(source / "planned.json", runs / run_id / "planned.json")
            (runs / fixture.INV_RUN).mkdir(parents=True)
            _write(runs / fixture.INV_RUN / "run.json",
                   {"run_id": fixture.INV_RUN, "mode": vocab.MODE_INVENTORY,
                    "locate": {"mode": vocab.MODE_INVENTORY,
                               "status_rule": vocab.STATUS_RULE_BROAD}})
            import index as index_mod  # noqa: PLC0415

            built = tally_mod.build(
                tally_mod.Workspace(corpus, runs, ws.ss.dir / "sources"), fixture.RUN,
                universe=tally_mod.Universe.load(ws.universe),
                declarations=index_mod.JsonlIndex.load(ws.index))
            self.assertEqual(built["counts"]["problems"], 0)

            # --- 9d: the challenge plan over those rows
            plan = challenge_mod.plan(
                {fixture.RUN: built["outcomes"]}, seed=0,
                status_rule=vocab.STATUS_RULE_BROAD,
                passages=cut_mod.passage_index(corpus / fixture.INV_RUN))
            self.assertTrue(plan["records"])
            document = challenge_mod.plan_document("chal-test-1", plan,
                                                   challenged_runs=[fixture.RUN])
            self.assertEqual(document["schema"], vocab.CHALLENGE_PLAN_SCHEMA)
            self.assertTrue(any(r["planned"] == vocab.CHALLENGE_REQUIRED
                                for r in document["records"]))
            manifests = challenge_mod.task_documents(
                "chal-test-1", plan,
                challenge_mod.read_sources(
                    ws.ss.manifest_path(ws.ss.latest().version)))
            self.assertTrue(manifests)
            for record in manifests[0]["documents"][0]["records"]:
                self.assertIn("quote", record)   # the cut's, carried from the inventory

            # nothing was paid for: every call was a local command
            for path in (inv_root / "usage").glob("*.json"):
                self.assertIsNone(json.loads(path.read_text())["total_cost_usd"])

    def test_the_same_run_planned_from_a_sealed_statement_set(self):
        """R16 again, from a **published** cut: 9b -> 6b -> 9c -> the gate -> harvest ->
        the tally -> 6, with the statement set standing where the harvested run stood.

        What it holds: the locate task manifests are **byte for byte** those of a run
        planned from the harvested cut, so nothing downstream knows which it was handed;
        and the record set's ``passage_key`` on every record is the statement set's, so
        a tag written on a statement in phase 1 is the row the location fills in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws")
            corpus = root / "corpus"
            runs = root / "runs"
            cut_dir = self._cut(root, ws, corpus)

            # --- 6b: seal the harvested cut as a statement set
            report = export_statements.from_cut(
                inventory_dir=cut_dir, sourceset_dir=ws.ss.dir, out=root / "set",
                set_id="stmt-test", campaign=f"run:{fixture.INV_RUN}",
                producer_commit="ab" * 20, created="2026-11-12T00:00:00Z")
            self.assertEqual(report["refused"], [])
            sealed = statements_mod.read_set(root / "set")

            # --- 9c: the same tasks, from the published cut and from the harvested one
            plans = {}
            for name, over in (("harvested", {"inventory": cut_dir}),
                               ("published", {"statement_set": root / "set"})):
                plans[name] = locate_mod.plan_locate(
                    run_id=fixture.RUN, out=root / f"loc-{name}", sourceset_dir=ws.ss.dir,
                    universe=ws.universe, index=ws.index, candidates=ws.candidates,
                    retrieve=True, **over)
            # Everything but the quote is identical, task for task and passage for
            # passage. The quote differs by design: a harvested cut hands the worker the
            # words IT was cut at, and a published one hands the format's capped head
            # (records.cut_passage), which is the quote the record will carry and the
            # reader already showed. So a run from a harvested cut is unchanged.
            quotes = {}
            for name, plan in plans.items():
                for task in plan["tasks"]:
                    for document in task["documents"]:
                        for passage in document["passages"]:
                            quotes.setdefault(name, {})[passage["item"]] = \
                                passage.pop("quote")
            self.assertEqual(plans["harvested"], plans["published"])
            self.assertNotEqual(quotes["harvested"], quotes["published"])
            for item, quote in quotes["published"].items():
                span = next(p["span"] for p in plans["published"]["tasks"][0]["documents"][0]
                            ["passages"] if p["item"] == item)
                self.assertEqual(quote,
                                 dict(records_mod.cut_passage(fixture.LAYER, span).quote))
            manifests = {name: [json.loads(path.read_text(encoding="utf-8"))
                                for path in sorted((root / f"loc-{name}" / "tasks")
                                                   .glob("*.json"))]
                         for name in ("harvested", "published")}
            for plan in manifests.values():
                for task in plan:
                    for document in task["documents"]:
                        for passage in document["passages"]:
                            passage.pop("quote")
            self.assertEqual(manifests["harvested"], manifests["published"])
            # run.json says which cut it was planned from, and nothing else differs
            configs = {name: json.loads((root / f"loc-{name}" / "run.json")
                                        .read_text(encoding="utf-8"))
                       for name in ("harvested", "published")}
            self.assertIsNone(configs["harvested"]["locate"]["statement_set"])
            self.assertEqual(configs["published"]["locate"]["statement_set"],
                             {"set_id": "stmt-test", "seal_sha256": sealed.seal_sha256})
            # The staged gate pins each task manifest by sha, and those differ in the
            # quote alone (above); gate.json itself, and everything else the gate block
            # names, is the same.
            staged = {name: configs[name]["gate"].pop("files")
                      for name in ("harvested", "published")}
            self.assertEqual({k: v for k, v in staged["harvested"].items()
                              if not k.startswith("tasks/")},
                             {k: v for k, v in staged["published"].items()
                              if not k.startswith("tasks/")})
            self.assertEqual(sorted(staged["harvested"]), sorted(staged["published"]))
            for config in configs.values():
                config["locate"].pop("statement_set")
            self.assertEqual(configs["harvested"], configs["published"])

            # --- the gate, the harvest and the tally over the published plan
            loc_root = root / "loc-published"
            loc_task = sorted(path.stem for path in (loc_root / "tasks").glob("*.json"))[0]
            document = fixture.good_locate_document()
            document["task_id"] = loc_task
            handed = {p["item"] for p in json.loads(
                (loc_root / "tasks" / f"{loc_task}.json").read_text(encoding="utf-8")
            )["documents"][0]["passages"]}
            document["passages"] = [p for p in document["passages"] if p["item"] in handed]
            self._stub(root, loc_root, loc_task, document, fixture.RUN)
            worktree = loc_root / "work" / loc_task
            self.assertEqual(
                gate.main(["--gate-config", str(loc_root / "gate" / "gate.json"),
                           "--root", str(worktree),
                           "--findings-dir", str(worktree / "findings")],
                          env={"TEND_TASK_ID": loc_task}), gate.EXIT_OK)
            receipt = harvest_mod.harvest(
                run_id=fixture.RUN, source=worktree / "findings", dest=corpus / fixture.RUN,
                usage=loc_root / "usage", gate_log=loc_root / "gate-log",
                run_config=json.loads((loc_root / "run.json").read_text(encoding="utf-8")))
            self.assertTrue(receipt["complete"], harvest_mod.describe(receipt))
            (runs / fixture.RUN).mkdir(parents=True)
            for name in ("run.json", "planned.json"):
                shutil.copyfile(loc_root / name, runs / fixture.RUN / name)
            (runs / fixture.INV_RUN).mkdir(parents=True)
            _write(runs / fixture.INV_RUN / "run.json",
                   {"run_id": fixture.INV_RUN, "mode": vocab.MODE_INVENTORY,
                    "locate": {"mode": vocab.MODE_INVENTORY,
                               "status_rule": vocab.STATUS_RULE_BROAD}})
            import index as index_mod  # noqa: PLC0415

            workspace = tally_mod.Workspace(corpus, runs, ws.ss.dir / "sources")
            built = tally_mod.build(workspace, fixture.RUN,
                                    universe=tally_mod.Universe.load(ws.universe),
                                    declarations=index_mod.JsonlIndex.load(ws.index))
            self.assertEqual(built["counts"]["problems"], 0)

            # --- 6: the record set, its cut taken from the published one
            rows, refused = tally_mod.export_rows(built, workspace)
            self.assertEqual(refused, [])
            # No challenge run here (9d is the test above), and nothing leaves the
            # pipeline without a challenge value: stamp the one a plan gives a record no
            # rule requires. What this test is about is the cut and the keys.
            for row in rows:
                for part in (row, *(row.get("locations") or []), row.get("absence"),
                             row.get("coverage")):
                    if isinstance(part, dict) and not part.get("challenge"):
                        part["challenge"] = vocab.CHALLENGE_NOT_REQUIRED
            statements_mod.frozen_inventory(sealed, ws.ss.dir / "sources")
            exported = export_records.build_set(
                rows=rows, sources=[entry()], universe=UNIVERSE, set_id="rec-test",
                campaign=f"run:{fixture.RUN}", sourceset=ws.ss.id,
                sourceset_manifest="cd" * 32, producer_commit="ab" * 20,
                created="2026-11-12T01:00:00Z", status_rule=vocab.STATUS_RULE_BROAD,
                layers={fixture.SOURCE: fixture.LAYER}, statement_cuts=sealed.cuts(),
                statement_spans=sealed.spans(), out=root / "recordset")
            self.assertEqual(exported["refused"], [])
            published = {row["item"]: row["passage_key"]
                         for row in sealed.statements[fixture.SOURCE]}
            keyed = 0
            for line in (root / "recordset" / "records" / f"{fixture.SOURCE}.jsonl"
                         ).read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                ext = record["extensions"][vocab.LOCATE]
                self.assertEqual(ext["passage_key"], published[ext["item"]])
                keyed += 1
            self.assertEqual(keyed, exported["exported"]["records"])

    def test_a_row_whose_cut_is_not_the_published_one_is_refused(self):
        """The check that makes the two phases one row: a row whose own cut disagrees
        with the sealed statement is refused by name, rather than re-keying the passage."""
        cuts = {(fixture.SOURCE, "In-text #00000001"):
                records_mod.Cut({"exact": "a", "prefix": "", "suffix": ""}, "aa" * 32)}
        row = {"source_id": fixture.SOURCE, "item": "In-text #00000001",
               "cut": {"quote": {"exact": "b", "prefix": "", "suffix": ""},
                       "span_sha256": "bb" * 32}}
        with self.assertRaises(records_mod.RecordProblem) as caught:
            export_records.row_cut(row, cuts)
        self.assertIn("the row's cut is not the published one", str(caught.exception))
        self.assertEqual(export_records.row_cut({**row, "cut": None}, cuts),
                         cuts[(fixture.SOURCE, "In-text #00000001")])
        with self.assertRaises(records_mod.RecordProblem) as caught:
            export_records.row_cut({**row, "item": "In-text #00000002"}, cuts)
        self.assertIn("the statement set holds no statement", str(caught.exception))

    def test_a_row_whose_span_is_not_the_published_one_is_refused(self):
        """A record carries its ROW's span beside the set's span_sha256 (records.subject_end),
        so the span is held to the sealed one too — including for a row with no cut of its
        own, which takes the sealed quote and hash unquestioned."""
        key = (fixture.SOURCE, "In-text #00000001")
        cuts = {key: records_mod.Cut({"exact": "a", "prefix": "", "suffix": ""}, "aa" * 32)}
        spans = {key: [10, 40]}
        row = {"source_id": fixture.SOURCE, "item": "In-text #00000001", "span": [10, 40],
               "cut": {"quote": {"exact": "a", "prefix": "", "suffix": ""},
                       "span_sha256": "aa" * 32}}
        self.assertEqual(export_records.row_cut(row, cuts, spans), cuts[key])
        for over in ({"span": [10, 41]}, {"span": None}, {"span": [10, 40], "cut": None}):
            moved = {**row, **over}
            if moved.get("span") == [10, 40]:
                continue
            with self.subTest(span=moved.get("span")):
                with self.assertRaises(records_mod.RecordProblem) as caught:
                    export_records.row_cut(moved, cuts, spans)
                self.assertIn("is not the published", str(caught.exception))
        # A row with no cut of its own still has to be at the sealed span.
        with self.assertRaises(records_mod.RecordProblem) as caught:
            export_records.row_cut({**row, "cut": None, "span": [0, 7]}, cuts, spans)
        self.assertIn("is not the published", str(caught.exception))
        self.assertEqual(export_records.row_cut({**row, "cut": None}, cuts, spans), cuts[key])
        # Without the set's spans the row's own span stands, as it always has.
        self.assertEqual(export_records.row_cut({**row, "span": [0, 7]}, cuts), cuts[key])

    def test_a_locate_run_is_planned_from_one_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws")
            for over in ({}, {"inventory": root / "a", "statement_set": root / "b"}):
                with self.subTest(over=sorted(over)):
                    with self.assertRaises(locate_mod.LocateProblem) as caught:
                        locate_mod.plan_locate(run_id=fixture.RUN, out=root / "loc",
                                               sourceset_dir=ws.ss.dir,
                                               universe=ws.universe, **over)
                    self.assertIn("planned from one cut", str(caught.exception))

    def test_a_sourceset_that_moved_on_from_the_sealed_cut_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root / "ws")
            corpus = root / "corpus"
            cut_dir = self._cut(root, ws, corpus)
            export_statements.from_cut(
                inventory_dir=cut_dir, sourceset_dir=ws.ss.dir, out=root / "set",
                set_id="stmt-test", campaign=f"run:{fixture.INV_RUN}",
                producer_commit="ab" * 20, created="2026-11-12T00:00:00Z")
            # another sourceset, pinning other bytes of the same page
            other = sourceset_mod.Sourceset.create(root / "ss-other", "ss-other", "enwiki",
                                                   created="2026-01-01T00:00:00Z")
            other.write_version({fixture.SOURCE: entry(html_sha256="e" * 64)},
                                created="2026-01-02T00:00:00Z")
            shutil.copytree(ws.ss.dir / "sources", other.dir / "sources")
            with self.assertRaises(locate_mod.LocateProblem) as caught:
                locate_mod.plan_locate(run_id=fixture.RUN, out=root / "loc",
                                       statement_set=root / "set", sourceset_dir=other.dir,
                                       universe=ws.universe)
            self.assertIn("the set was cut from html_sha256", str(caught.exception))

    def _stub(self, root: Path, run_root: Path, task: str, document: dict,
              run_id: str) -> None:
        """A worker that is a local command, run through ``run.agent``."""
        document = {**document, "run_id": run_id, "task_id": task}
        worktree = run_root / "work" / task
        (worktree / "findings" / task).mkdir(parents=True, exist_ok=True)
        out = worktree / "findings" / task / f"{fixture.SOURCE}.json"
        script = root / f"stub-{task}.py"
        script.write_text(
            "import pathlib\n"
            f"pathlib.Path({str(out)!r}).write_text({json.dumps(document, sort_keys=True)!r},"
            " encoding='utf-8')\n", encoding="utf-8")
        envelope = agent_mod.call(root=run_root, task_id=task, role="worker", prompt="",
                                  worker_command=f"{sys.executable} {script}")
        assert envelope["agent_call"]["exit"] == 0, envelope


if __name__ == "__main__":
    unittest.main(verbosity=2)
