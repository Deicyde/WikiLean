#!/usr/bin/env python3
"""Tests for scripts/atlas/build_store.py, the atlas store builder (docs/ATLAS.md).

New code. Runs offline on the hand-built fixture under scripts/atlas/fixtures/:
the committed record set must equal what make_fixture.py writes, every record must
pass the vendored Literature Links schema, the committed golden store must equal a
fresh build, and each broken variant of the record set must be refused with a
message naming the rule it breaks. The Worker tests (wiki/test/atlas.test.ts) read
the same golden store, so the two languages share one contract fixture.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))

import build_store  # noqa: E402
import make_fixture  # noqa: E402

RECORDSET = HERE / "fixtures" / "recordset"
STORE = HERE / "fixtures" / "store"
PRODUCER_TABLE = HERE / "fixtures" / "producer_derive_status.json"
LOCATE = build_store.LOCATE


def reseal(set_dir: Path) -> None:
    """Recompute SEAL.json's file map after a test edits the copy (what a producer would do)."""
    seal_path = set_dir / "SEAL.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["files"] = {p.relative_to(set_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(set_dir.rglob("*")) if p.is_file() and p.name != "SEAL.json"}
    seal_path.write_text(json.dumps(seal, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(make_fixture.dumps(row) + "\n" for row in rows), encoding="utf-8")


class FixtureTests(unittest.TestCase):
    def test_committed_record_set_is_what_make_fixture_writes(self):
        expected = make_fixture.files()
        on_disk = {p.relative_to(RECORDSET).as_posix(): p.read_bytes()
                   for p in RECORDSET.rglob("*") if p.is_file()}
        self.assertEqual(sorted(on_disk), sorted(expected))
        for path, data in expected.items():
            self.assertEqual(on_disk[path], data, f"{path} differs; rerun make_fixture.py")

    def test_every_record_passes_the_vendored_schema(self):
        from jsonschema import Draft202012Validator
        schema_path = ROOT / "wiki/src/vendor/literature-links/companion.schema.json"
        validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        records = [r for p in sorted((RECORDSET / "records").glob("*.jsonl")) for r in read_jsonl(p)]
        self.assertEqual(len(records), 7)
        for record in records:
            errors = list(validator.iter_errors(record))
            self.assertEqual(errors, [], record["id"])

    def test_fixture_covers_the_required_outcomes(self):
        records = read_jsonl(RECORDSET / "records/enwiki-1001-2002.jsonl")
        shapes = {(r.get("relation"), r.get("status"),
                   r["object"][0].get("extensions", {}).get(LOCATE, {}).get("library") if r["object"] else None,
                   r["object"][0].get("extensions", {}).get(LOCATE, {}).get("proof_status") if r["object"] else None)
                  for r in records}
        self.assertIn(("exactMatch", "formalized", "mathlib", "proved"), shapes)
        self.assertIn(("closeMatch", "formalized", "ff-widgets", "proved"), shapes)
        self.assertIn(("exactMatch", "statement_only", "tauceti", "statement_only"), shapes)
        self.assertIn(("noMatch", "not_formalized", None, None), shapes)
        self.assertIn((None, None, None, None), shapes)  # unresolved: object [] and no status
        absence = next(r for r in records if r.get("relation") == "noMatch")
        self.assertEqual(len(absence["object"]), 5)
        self.assertTrue(all(set(end) == {"type", "uri", "version"} for end in absence["object"]))


class BuildTests(unittest.TestCase):
    def test_golden_store_matches_a_fresh_build(self):
        tree = build_store.build(RECORDSET)
        self.assertEqual(build_store.check_tree(STORE, tree), [])

    def test_build_is_deterministic_and_built_at_only_touches_current(self):
        first = build_store.build(RECORDSET)
        self.assertEqual(first, build_store.build(RECORDSET))
        later = build_store.build(RECORDSET, built_at="2026-10-18T00:00:00Z")
        changed = sorted(p for p in first if first[p] != later[p])
        self.assertEqual(changed, ["atlas/v1/current.json", "atlas/v1/manifest.json"])
        with self.assertRaises(build_store.AtlasError):
            build_store.build(RECORDSET, built_at="yesterday")

    def test_manifest_lists_every_other_file(self):
        tree = build_store.build(RECORDSET)
        manifest = json.loads(tree["atlas/v1/manifest.json"])
        self.assertEqual(sorted(manifest["files"]), sorted(p for p in tree if p != "atlas/v1/manifest.json"))
        for path, entry in manifest["files"].items():
            self.assertEqual(entry["sha256"], hashlib.sha256(tree[path]).hexdigest())
            self.assertEqual(entry["bytes"], len(tree[path]))

    def test_records_are_copied_verbatim(self):
        tree = build_store.build(RECORDSET)
        self.assertEqual(tree["atlas/v1/records/1001.jsonl"],
                         (RECORDSET / "records/enwiki-1001-2002.jsonl").read_bytes())
        article = json.loads(tree["atlas/v1/articles/1001.json"])
        self.assertEqual(article["records_sha256"],
                         hashlib.sha256(tree["atlas/v1/records/1001.jsonl"]).hexdigest())

    def test_article_view_model(self):
        article = json.loads(build_store.build(RECORDSET)["atlas/v1/articles/1001.json"])
        self.assertEqual(article["schema"], "wikilean.atlas.article/1")
        self.assertEqual((article["pageid"], article["title"], article["revision"]), (1001, "Widget lemma", 2002))
        self.assertEqual(article["campaign"], "fixture-2026-10")
        self.assertEqual([p["status"] for p in article["passages"]],
                         ["formalized", "statement_only", "not_formalized", None])
        self.assertEqual([p["outcome"] for p in article["passages"]], ["located", "located", "absent", "unresolved"])
        first = article["passages"][0]
        self.assertEqual([loc["library"] for loc in first["locations"]], ["mathlib", "ff-widgets"])
        self.assertEqual(first["locations"][0]["line"], 612)
        self.assertEqual(first["section"], ["Statement"])
        self.assertEqual(first["prominence"], "central")
        absent = article["passages"][2]
        self.assertEqual(absent["absence"]["libraries"], 5)
        self.assertEqual(absent["absence"]["universe"], "u-fixture")
        self.assertEqual(absent["locations"][0]["relation"], "relatedMatch")
        self.assertEqual(article["passages"][3]["unresolved"], {"challenge": "not_required"})
        # The basis is the location that justifies the status; absent and unresolved have none.
        self.assertEqual([p["basis"] for p in article["passages"]],
                         [first["locations"][0]["location_key"], article["passages"][1]["locations"][0]["location_key"],
                          None, None])
        summary = article["summary"]
        self.assertEqual(summary["by_status"], {"formalized": 1, "statement_only": 1, "partial": 0,
                                                "not_formalized": 1, "unresolved": 1})
        self.assertEqual(summary["by_library"]["batteries"],
                         {"locations": 0, "complete": 0, "incomplete": 0, "passages": 0, "related": 1})
        self.assertEqual(summary["by_library"]["mathlib"],
                         {"locations": 1, "complete": 1, "incomplete": 0, "passages": 1, "related": 0})
        # A statement-only location is a location, but not a complete one.
        self.assertEqual(summary["by_library"]["tauceti"],
                         {"locations": 1, "complete": 0, "incomplete": 1, "passages": 1, "related": 0})
        # The small article follows proposal §2.7 exactly: no section or prominence.
        gadget = json.loads(build_store.build(RECORDSET)["atlas/v1/articles/1003.json"])
        self.assertIsNone(gadget["passages"][0]["section"])
        self.assertIsNone(gadget["passages"][0]["prominence"])

    def test_index_and_title_keys(self):
        self.assertEqual(build_store.title_key("Widget_lemma"), "Widget lemma")
        self.assertEqual(build_store.title_key("  Gadget\u00a0(mathematics) "), "Gadget (mathematics)")
        # wiki/test/atlas.test.ts pins the same shards for the same titles.
        self.assertEqual(build_store.title_shard("Widget lemma"), "1")
        self.assertEqual(build_store.title_shard("Gadget (mathematics)"), "8")
        tree = build_store.build(RECORDSET)
        current = json.loads(tree["atlas/v1/current.json"])
        self.assertEqual(current["index"], {"hex": 1, "shards": ["1", "8"], "entries": 2})
        shard = json.loads(tree["atlas/v1/index/1.json"])
        # The last field lists the libraries holding a location, in universe order
        # (Batteries holds only a related declaration).
        self.assertEqual(shard["articles"], [["Widget lemma", 1001, 2002, 4, 1, 1, 0, 1, 1, "high",
                                              ["mathlib", "tauceti", "ff-widgets"]]])
        self.assertEqual(shard["fields"][0:3], ["title", "pageid", "revision"])
        self.assertEqual(shard["fields"][-1], "libraries")
        totals = current["counts"]["by_library"]
        self.assertEqual(totals["tauceti"], {"articles": 1, "passages": 1, "locations": 1, "complete": 0,
                                             "incomplete": 1, "related": 0})


class DeriveStatusParityTests(unittest.TestCase):
    """build_store.derive_status against the producer's own answers (make_producer_table.py)."""

    def setUp(self):
        self.table = json.loads(PRODUCER_TABLE.read_text(encoding="utf-8"))

    def test_table_is_the_producers(self):
        self.assertEqual(self.table["producer"]["repository"], "formal-companion")
        self.assertRegex(self.table["producer"]["commit"], r"^[0-9a-f]{40}$")
        cases = self.table["cases"]
        singles = [case for case in cases if len(case[1]) == 1]
        # Every kind x relation x proof status x statement flag, once each.
        self.assertEqual(len(singles), 7 * 6 * 5 * 2)
        self.assertEqual(len({json.dumps(case[:2]) for case in singles}), len(singles))
        self.assertGreaterEqual(len(cases) - len(singles), 300)

    def test_builder_derives_exactly_the_producers_status(self):
        mismatches = []
        for kind, ends, expected in self.table["cases"]:
            problems = [build_store.statement_flag(statement, proof_status)[1] for _, proof_status, statement in ends]
            if expected == "refused":
                if not any(problems):
                    mismatches.append((kind, ends, expected, "accepted"))
                continue
            if any(problems):
                mismatches.append((kind, ends, expected, f"refused: {problems}"))
                continue
            locations = [{"relation": relation, "proof_status": proof_status, "statement": statement,
                          "rank": rank, "library": "lib", "location_key": f"key{rank}"}
                         for rank, (relation, proof_status, statement) in enumerate(ends, 1)]
            status, basis = build_store.derive_status(kind, locations)
            if status != expected:
                mismatches.append((kind, ends, expected, status))
            elif status is None:
                self.assertIsNone(basis)
            else:
                # The basis is the highest-ranked location that reaches the passage's status.
                reaching = [loc for loc in locations if build_store.location_status(kind, loc) == status]
                self.assertIs(basis, reaching[0], (kind, ends))
        self.assertEqual(mismatches, [], f"{len(mismatches)} cases differ from the producer")


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-test-"))
        self.set = self.tmp / "set"
        shutil.copytree(RECORDSET, self.set)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def refuse(self, fragment: str):
        with self.assertRaises(build_store.AtlasError) as caught:
            build_store.build(self.set)
        self.assertIn(fragment, str(caught.exception))

    def edit_widget(self, edit) -> None:
        path = self.set / "records/enwiki-1001-2002.jsonl"
        rows = read_jsonl(path)
        edit(rows)
        write_jsonl(path, rows)
        reseal(self.set)

    def test_sha_mismatch_without_reseal(self):
        path = self.set / "sources.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Widget lemma", b"Widget lemmb"))
        self.refuse("sha256 does not match SEAL.json")

    def test_unlisted_file(self):
        (self.set / "records" / "extra.txt").write_text("x\n")
        self.refuse("present but not listed in SEAL.json")

    def test_schema_invalid_record(self):
        self.edit_widget(lambda rows: rows[0].update(status={"value": "formalized"}))
        self.refuse("Literature Links $.status")

    def test_absence_missing_a_library(self):
        self.edit_widget(lambda rows: rows[3]["object"].pop())
        self.refuse("names every universe library")

    def test_unresolved_with_a_status(self):
        self.edit_widget(lambda rows: rows[5].update(status="not_formalized"))
        self.refuse("unresolved record has object [] and no status")

    def test_duplicate_location_key(self):
        def edit(rows):
            key = rows[0]["extensions"][LOCATE]["location_key"]
            rows[2]["extensions"][LOCATE]["location_key"] = key
        self.edit_widget(edit)
        self.refuse("appears twice")

    def test_quote_over_the_cap(self):
        def edit(rows):
            for row in rows[:2]:
                row["subject"][0]["location"]["quote"]["exact"] = " ".join(["word"] * 13)
        self.edit_widget(edit)
        self.refuse("exceeds 12 words")

    def test_commit_outside_the_universe(self):
        self.edit_widget(lambda rows: rows[0]["object"][0].update(version="0" * 40))
        self.refuse("uri and version must be its library's repo and commit")

    def test_record_names_another_universe(self):
        self.edit_widget(lambda rows: rows[0]["extensions"][LOCATE].update(universe_sha256="f" * 64))
        self.refuse("must name the record set's universe")

    def test_formalized_needs_a_proved_strong_match(self):
        def edit(rows):
            for row in rows[:2]:
                row["object"][0]["extensions"][LOCATE]["proof_status"] = "statement_only"
        self.edit_widget(edit)
        self.refuse("the records say formalized, but proposal §2.6 derives statement_only for a theorem passage")

    def _first_passage(self, edit) -> None:
        """Edit the two records of the first Widget passage (Mathlib rank 1, FormalFrontier rank 2)."""
        self.edit_widget(lambda rows: edit(rows[0], rows[1]))

    def test_formalized_example_needs_a_complete_match(self):
        # Any kind, not only theorem and definition: a sorry-backed match never formalizes.
        def edit(a, b):
            for row in (a, b):
                row["kind"] = "example"
                row["object"][0]["extensions"][LOCATE]["proof_status"] = "statement_only"
        self._first_passage(edit)
        self.refuse("the records say formalized, but proposal §2.6 derives statement_only for a example passage")

    def test_formalized_construction_via_a_wanted_declaration(self):
        # A construction is judged as a definition passage (vocabulary.md §1.11), whose
        # broadMatch is partial whatever the declaration is.
        def edit(a, b):
            for row in (a, b):
                row["kind"] = "construction"
                row["relation"] = "broadMatch"
                row.pop("note", None)
                row["object"][0]["extensions"][LOCATE]["proof_status"] = "wanted"
        self._first_passage(edit)
        self.refuse("derives partial for a construction passage with broadMatch to wanted")

    def test_example_with_a_proved_match_is_formalized(self):
        def edit(a, b):
            for row in (a, b):
                row["kind"] = "example"
        self._first_passage(edit)
        article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
        self.assertEqual(article["passages"][0]["status"], "formalized")

    def test_understated_status_is_refused(self):
        # A theorem with a proved exact match is formalized, not statement_only.
        self._first_passage(lambda a, b: [row.update(status="statement_only") for row in (a, b)])
        self.refuse("the records say statement_only, but proposal §2.6 derives formalized")

    def test_partial_beside_a_proved_exact_match_is_refused(self):
        def edit(a, b):
            b["relation"] = "hasPart"
            b.pop("note", None)
            for row in (a, b):
                row["status"] = "partial"
        self._first_passage(edit)
        self.refuse("the records say partial, but proposal §2.6 derives formalized")

    def _related_only(self, a, b, status=None):
        for row in (a, b):
            row["relation"] = "relatedMatch"
            row.pop("note", None)
            row["extensions"][LOCATE]["challenge"] = "downgraded"
            if status is None:
                row.pop("status", None)
            else:
                row["status"] = status

    def test_located_passage_with_only_related_matches_has_no_status(self):
        # Records that stamp a status on a related-only passage are refused: the table gives none.
        self._first_passage(lambda a, b: self._related_only(a, b, "partial"))
        self.refuse("gives a theorem passage with relatedMatch only no status: it is unresolved")

    def test_related_only_passage_without_a_status_is_unresolved(self):
        # Proposal §2.6 "(none)": what a challenge that downgrades a passage's locations leaves.
        # It counts as undecided, never as not formalized, and keeps its related declarations.
        self._first_passage(self._related_only)
        tree = build_store.build(self.set)
        article = json.loads(tree["atlas/v1/articles/1001.json"])
        passage = article["passages"][0]
        self.assertEqual((passage["outcome"], passage["status"], passage["basis"]), ("unresolved", None, None))
        self.assertEqual(passage["unresolved"], {"challenge": "downgraded"})
        self.assertEqual([loc["relation"] for loc in passage["locations"]], ["relatedMatch", "relatedMatch"])
        self.assertEqual(article["summary"]["by_status"]["unresolved"], 2)
        self.assertEqual(article["summary"]["by_status"]["formalized"], 0)
        self.assertEqual(article["summary"]["by_library"]["mathlib"],
                         {"locations": 0, "complete": 0, "incomplete": 0, "passages": 0, "related": 1})
        shard = json.loads(tree["atlas/v1/index/1.json"])
        self.assertEqual(shard["articles"][0][-1], ["tauceti"])  # Mathlib and FormalFrontier hold related only

    def test_status_less_related_match_beside_a_counted_location_is_refused(self):
        def edit(a, b):
            b["relation"] = "relatedMatch"
            b.pop("note", None)
            b.pop("status")
        self._first_passage(edit)
        self.refuse("every record of a passage carries the same derived status (only a passage whose")

    def test_status_less_record_that_counts_is_refused(self):
        self._first_passage(lambda a, b: [row.pop("status") for row in (a, b)])
        self.refuse("a location record carries a derived status")

    def test_status_less_related_match_beside_an_absence_is_refused(self):
        self.edit_widget(lambda rows: rows[4].pop("status"))
        self.refuse("every record of a passage carries the same derived status")

    def test_broad_match_to_a_definition_is_partial(self):
        # The table's "broadMatch to a definition" row: partial, and only partial. A
        # Prop-valued definition is marked by the statement flag, not by the relation.
        for status, refused in (("partial", False), ("statement_only", True)):
            with self.subTest(status=status):
                self.tearDown()
                self.setUp()

                def edit(a, b, status=status):
                    for row in (a, b):
                        row["relation"] = "broadMatch"
                        row.pop("note", None)
                        row["status"] = status
                        row["object"][0]["extensions"][LOCATE]["proof_status"] = "definition"
                self._first_passage(edit)
                if refused:
                    self.refuse("the records say statement_only, but proposal §2.6 derives partial")
                else:
                    article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
                    self.assertEqual(article["passages"][0]["status"], "partial")

    def _one_location(self, kind, relation, proof_status, status, statement=None):
        """Make the first Widget passage one location (Mathlib, rank 1) of the given shape."""
        def edit(rows):
            del rows[1]
            row = rows[0]
            row["kind"], row["relation"], row["status"] = kind, relation, status
            if relation != "closeMatch":
                row.pop("note", None)
            end = row["object"][0]["extensions"][LOCATE]
            end["proof_status"] = proof_status
            if statement is not None:
                end["statement"] = statement
        self.edit_widget(edit)

    def test_theorem_exactly_matched_to_data_is_partial(self):
        # An instance such as Real.instField stating "R is a field": vocabulary.md §1.11
        # decides it partial (as the table's broadMatch to a definition is).
        self._one_location("theorem", "exactMatch", "definition", "partial")
        self.assertEqual(json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
                         ["passages"][0]["status"], "partial")
        self.tearDown()
        self.setUp()
        self._one_location("theorem", "exactMatch", "definition", "statement_only")
        self.refuse("the records say statement_only, but proposal §2.6 derives partial for a theorem passage "
                    "with exactMatch to definition")

    def test_prop_valued_definition_states_a_theorem(self):
        # The lean end's statement flag marks a Prop-valued definition: statement_only.
        self._one_location("theorem", "exactMatch", "definition", "statement_only", statement=True)
        article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
        passage = article["passages"][0]
        self.assertEqual(passage["status"], "statement_only")
        self.assertIs(passage["locations"][0]["statement"], True)
        self.assertEqual(passage["basis"], passage["locations"][0]["location_key"])
        self.tearDown()
        self.setUp()
        self._one_location("theorem", "exactMatch", "definition", "partial", statement=True)
        self.refuse("derives statement_only for a theorem passage with exactMatch to a Prop-valued definition")

    def test_statement_flag_must_be_a_bool_and_never_on_a_proved_declaration(self):
        self._one_location("theorem", "exactMatch", "definition", "statement_only", statement="yes")
        self.refuse("statement must be true or false")
        self.tearDown()
        self.setUp()
        self._one_location("theorem", "exactMatch", "proved", "formalized", statement=True)
        self.refuse("a proved declaration with the statement flag set")

    def test_definition_matched_to_a_proved_theorem_is_partial(self):
        for relation in ("exactMatch", "broadMatch"):
            with self.subTest(relation=relation):
                self.tearDown()
                self.setUp()
                self._one_location("definition", relation, "proved", "partial")
                self.assertEqual(json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
                                 ["passages"][0]["status"], "partial")

    def test_construction_and_notation_are_definition_passages(self):
        # A proved theorem never formalizes a construction or a notation (vocabulary.md §1.11).
        for kind in ("construction", "notation"):
            with self.subTest(kind=kind):
                self.tearDown()
                self.setUp()
                self._one_location(kind, "exactMatch", "proved", "formalized")
                self.refuse(f"the records say formalized, but proposal §2.6 derives partial for a {kind} passage")

    def test_basis_is_the_location_that_justifies_the_status(self):
        # Rank 1 states it without proof, rank 2 proves a more general result:
        # the passage is formalized, and the basis is rank 2, not rank 1.
        def edit(a, b):
            a["object"][0]["extensions"][LOCATE]["proof_status"] = "statement_only"
            b["relation"] = "broadMatch"
            b.pop("note", None)
        self._first_passage(edit)
        article = json.loads(build_store.build(self.set)["atlas/v1/articles/1001.json"])
        passage = article["passages"][0]
        self.assertEqual(passage["status"], "formalized")
        self.assertEqual(passage["basis"], passage["locations"][1]["location_key"])
        self.assertEqual(passage["locations"][1]["library"], "ff-widgets")

    def test_unknown_status_is_a_refusal_not_a_crash(self):
        self.edit_widget(lambda rows: [row.update(status="disputed") for row in rows[:2]])
        self.refuse("a location record carries a derived status")

    def test_non_string_affix_is_a_refusal_not_a_crash(self):
        def edit(rows):
            for row in rows:
                row["subject"][0]["location"] = {"profile": "https://example.org/p",
                                                 "quote": {"exact": "a widget", "prefix": 5}}
        self.edit_widget(edit)
        self.refuse("quote.prefix must be a string")

    def test_subject_uri_must_name_exactly_the_revision(self):
        def edit(rows):
            for row in rows:
                row["subject"][0]["uri"] = row["subject"][0]["uri"].replace("oldid=2002", "oldid=20029")
        self.edit_widget(edit)
        self.refuse("oldid is exactly the revision")

    def test_two_pages_with_one_title_key(self):
        path = self.set / "sources.jsonl"
        rows = read_jsonl(path)
        rows[1]["title"] = "Widget_lemma"
        write_jsonl(path, rows)
        reseal(self.set)
        self.refuse("has the same title key as page 1001")

    def edit_universe(self, edit) -> None:
        path = self.set / "universe.json"
        universe = json.loads(path.read_text())
        edit(universe)
        path.write_text(make_fixture.dumps(universe) + "\n")
        reseal(self.set)

    def test_blob_url_template_is_refused(self):
        self.edit_universe(lambda u: u["libraries"][0].update(
            blob_url_template="https://phish.example/login?next={commit}{path}"))
        self.refuse("blob_url_template is not supported")

    def test_repo_with_dot_segments_is_refused(self):
        self.edit_universe(lambda u: u["libraries"][4].update(repo="https://github.com/../.."))
        self.refuse("repo must be an https repository URL")

    def test_receipt_with_too_few_queries(self):
        receipts = list((self.set / "receipts").glob("*.json"))
        body = json.loads(receipts[0].read_text())
        body["queries"] = body["queries"][:2]
        data = (make_fixture.dumps(body) + "\n").encode()
        digest = hashlib.sha256(data).hexdigest()
        receipts[0].unlink()
        (self.set / "receipts" / f"{digest}.json").write_bytes(data)

        def edit(rows):
            rows[3]["extensions"][LOCATE]["receipt"] = {"sha256": digest, "queries": 2, "candidates": 2}
        self.edit_widget(edit)
        self.refuse("at least 3 distinct queries")

    def test_universe_disagrees_with_seal(self):
        path = self.set / "universe.json"
        universe = json.loads(path.read_text())
        universe["universe_sha256"] = "e" * 64
        path.write_text(make_fixture.dumps(universe) + "\n")
        reseal(self.set)
        self.refuse("universe_sha256 differs from SEAL.json")

    def write_yaml(self, text: str) -> None:
        (self.set / "universe.json").unlink()
        (self.set / "universe.yaml").write_text(text)
        reseal(self.set)

    def test_malformed_yaml_is_a_refusal(self):
        self.write_yaml("id: u-fixture\nlibraries: [\n")
        self.refuse("universe.yaml: not valid YAML")

    def test_duplicate_yaml_keys_are_refused(self):
        import yaml
        universe = json.loads((self.set / "universe.json").read_text())
        text = yaml.safe_dump(universe, sort_keys=True)
        self.write_yaml(text + "frozen: '2026-10-18'\n")
        self.refuse("duplicate key 'frozen'")

    def test_universe_yaml_is_accepted(self):
        import yaml
        universe = json.loads((self.set / "universe.json").read_text())
        (self.set / "universe.json").unlink()
        (self.set / "universe.yaml").write_text(yaml.safe_dump(universe, sort_keys=True))
        reseal(self.set)
        tree = build_store.build(self.set)
        golden = build_store.build(RECORDSET)
        universe_path = next(p for p in golden if p.startswith("atlas/v1/universes/"))
        self.assertEqual(tree[universe_path], golden[universe_path])


class MalformedValueTests(unittest.TestCase):
    """A malformed value anywhere is a refusal (exit 2), never a crash (docs/ATLAS.md, section 5)."""

    HOSTILE = [None, True, 5, 1.5, "x", [], ["a"], {}, {"a": [1]}]
    WIDGET = "records/enwiki-1001-2002.jsonl"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-malformed-"))
        self.set = self.tmp / "set"
        shutil.copytree(RECORDSET, self.set)
        self.original = {p: p.read_bytes() for p in self.set.rglob("*") if p.is_file()}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def restore(self) -> None:
        for path, data in self.original.items():
            path.write_bytes(data)

    @staticmethod
    def put(value, path, new):
        for step in path[:-1]:
            value = value[step]
        value[path[-1]] = new

    def mutate(self, relative: str, row: int | None, path: list, new) -> None:
        target = self.set / relative
        if row is None:
            document = json.loads(target.read_text(encoding="utf-8"))
            self.put(document, path, new)
            target.write_text(make_fixture.dumps(document) + "\n", encoding="utf-8")
        else:
            rows = read_jsonl(target)
            self.put(rows[row], path, new)
            write_jsonl(target, rows)
        reseal(self.set)

    def build_outcome(self) -> str:
        try:
            build_store.build(self.set)
        except build_store.AtlasError:
            return "refused"
        except Exception as exc:  # noqa: BLE001 - the point of the test
            return f"crashed: {type(exc).__name__}: {exc}"
        return "built"

    def test_no_malformed_value_crashes_the_builder(self):
        loc_end = ["object", 0, "extensions", LOCATE]
        targets = [(self.WIDGET, 0, path) for path in (
            ["kind"], ["relation"], ["status"], ["note"], ["id"],
            ["extensions", LOCATE, "challenge"], ["extensions", LOCATE, "confidence"],
            ["extensions", LOCATE, "rank"], ["extensions", LOCATE, "location_key"],
            ["extensions", LOCATE, "section"], ["extensions", LOCATE, "prominence"],
            ["extensions", LOCATE, "item"], ["extensions", LOCATE, "universe"],
            [*loc_end, "proof_status"], [*loc_end, "library"], [*loc_end, "path"], [*loc_end, "line"],
            [*loc_end, "end_line"], [*loc_end, "statement"], ["object", 0, "location"],
            ["object", 0, "location", "declaration"], ["object", 0, "uri"], ["object", 0, "version"],
            ["subject", 0, "uri"], ["subject", 0, "version"], ["subject", 0, "location", "quote"],
            ["subject", 0, "location", "quote", "exact"], ["subject", 0, "location", "quote", "prefix"],
            ["subject", 0, "extensions", LOCATE, "pageid"])]
        targets += [(self.WIDGET, 3, path) for path in (
            ["extensions", LOCATE, "receipt"], ["extensions", LOCATE, "receipt", "sha256"],
            ["extensions", LOCATE, "receipt", "queries"], ["object", 0, "uri"], ["object", 0, "version"])]
        targets += [("universe.json", None, path) for path in (
            ["excluded"], ["excluded", 0], ["excluded", 0, "id"], ["excluded", 0, "repo"], ["libraries"],
            ["libraries", 0], ["libraries", 0, "id"], ["libraries", 0, "repo"], ["libraries", 0, "commit"],
            ["libraries", 0, "display_name"], ["libraries", 0, "roots"], ["libraries", 0, "licence"],
            ["libraries", 0, "src_dir"], ["frozen"], ["id"], ["universe_sha256"])]
        targets += [("sources.jsonl", 0, path) for path in (
            ["renderer"], ["assessment"], ["assessment", "class"], ["title"], ["wikidata"], ["pageid"],
            ["html_sha256"], ["source_id"])]
        crashes = []
        for relative, row, path in targets:
            for value in self.HOSTILE:
                self.mutate(relative, row, path, value)
                outcome = self.build_outcome()
                if outcome.startswith("crashed"):
                    crashes.append((relative, row, path, value, outcome))
                self.restore()
        self.assertEqual(crashes, [])

    def run_main(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = build_store.main(["--record-set", str(self.set), "--out", str(self.tmp / "out")])
        return code, err.getvalue()

    def assert_exit_2(self, fragment: str) -> None:
        code, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn(fragment, err)
        self.assertFalse((self.tmp / "out").exists())
        self.restore()

    def test_reported_crashes_are_refusals(self):
        # Each of these used to exit 3 ("internal error") instead of listing the problem.
        for value in (5, True, {"id": "x"}):
            self.mutate("universe.json", None, ["excluded"], value)
            self.assert_exit_2("excluded must be a list")
        for path, fragment in ((["extensions", LOCATE, "challenge"], "challenge must be one of"),
                               (["extensions", LOCATE, "confidence"], "confidence must be one of"),
                               (["extensions", LOCATE, "prominence"], "prominence must be one of"),
                               (["object", 0, "extensions", LOCATE, "proof_status"], "proof_status must be one of"),
                               (["extensions", LOCATE, "rank"], "rank must be an integer")):
            self.mutate(self.WIDGET, 0, path, ["a"])
            self.assert_exit_2(fragment)
        self.mutate("sources.jsonl", 0, ["renderer"], {"x": [1]})
        self.assert_exit_2("renderer must be a short token")
        # 1e400 is valid JSON that parses as infinity, which the store cannot write.
        path = self.set / "sources.jsonl"
        path.write_text(path.read_text().replace('"renderer":"web-html/1"', '"renderer":1e400', 1))
        reseal(self.set)
        self.assert_exit_2("non-finite JSON number 1e400")
        path = self.set / self.WIDGET
        path.write_text(path.read_text().replace('"rank":1', '"rank":1e400', 1))
        reseal(self.set)
        self.assert_exit_2("non-finite JSON number 1e400")
        # Nesting too deep for the parser is a refusal too.
        path.write_text("[" * 100000 + "]" * 100000 + "\n" + path.read_text())
        reseal(self.set)
        self.assert_exit_2("nested too deeply")

    def test_display_name_is_printable_ascii(self):
        # Invisible and look-alike characters could make a name read as "Mathlib".
        for name in ("Mathlib\u200b", "M\u0430thlib", "Mathlib\u00ad", "Lean core\u2060", "Tab\there"):
            self.mutate("universe.json", None, ["libraries", 4, "display_name"], name)
            self.assert_exit_2("display_name must be at most 100 printable ASCII")
        self.mutate("universe.json", None, ["libraries", 4, "display_name"], "Widget theory (FF \u00b7 fork), v2")
        self.assertEqual(self.build_outcome(), "built")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = build_store.main(list(args))
        return code, err.getvalue()

    def test_write_check_and_refuse_overwrite(self):
        out = self.tmp / "store"
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out))[0], 0)
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--check")[0], 0)
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)
        target = out / "atlas/v1/articles/1001.json"
        target.write_bytes(target.read_bytes().replace(b"Widget", b"Wodget"))
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--check")
        self.assertEqual(code, 1)
        self.assertIn("differs: atlas/v1/articles/1001.json", err)
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--replace")[0], 0)
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--check")[0], 0)

    def test_replace_refuses_a_directory_holding_more_than_a_store(self):
        out = self.tmp / "public"
        self.assertEqual(self.run_main("--record-set", str(RECORDSET), "--out", str(out / "store"))[0], 0)
        (out / "precious.txt").write_text("keep me\n")
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out), "--replace")
        self.assertEqual(code, 2)
        self.assertIn("holds more than an atlas store (precious.txt, store)", err)
        self.assertEqual((out / "precious.txt").read_text(), "keep me\n")
        self.assertTrue((out / "store/atlas/v1/current.json").is_file())
        (out / "store/atlas/v0").mkdir()
        code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(out / "store"), "--replace")
        self.assertEqual(code, 2)
        self.assertIn("(atlas/v0)", err)

    def test_unknown_status_exits_2_not_1(self):
        broken = self.tmp / "set"
        shutil.copytree(RECORDSET, broken)
        path = broken / "records/enwiki-1003-2004.jsonl"
        path.write_text(path.read_text().replace('"status":"formalized"', '"status":"disputed"'))
        reseal(broken)
        code, err = self.run_main("--record-set", str(broken), "--out", str(STORE), "--check")
        self.assertEqual(code, 2)
        self.assertIn("a location record carries a derived status", err)

    def test_internal_error_exits_3(self):
        original = build_store.build
        build_store.build = lambda *args, **kwargs: {}["boom"]
        try:
            code, err = self.run_main("--record-set", str(RECORDSET), "--out", str(self.tmp / "o"))
        finally:
            build_store.build = original
        self.assertEqual(code, 3)
        self.assertIn("internal error, nothing was written (KeyError", err)
        self.assertFalse((self.tmp / "o").exists())

    def test_refused_set_exits_2_without_writing(self):
        broken = self.tmp / "set"
        shutil.copytree(RECORDSET, broken)
        (broken / "SEAL.json").unlink()
        out = self.tmp / "out"
        code, err = self.run_main("--record-set", str(broken), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("SEAL.json is missing", err)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
