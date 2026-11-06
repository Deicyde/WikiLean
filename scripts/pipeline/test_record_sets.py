#!/usr/bin/env python3
"""Tests for stages 4 and 6: a sealed record set read back, and written.

New code. The chain this exercises is the reproduction test docs/PIPELINE.md section 8
names: a sealed set is read back into outcome rows (stage 4), the rows are re-exported
(stages 5 and 6), and the result must be the sealed set **byte for byte**, SEAL.json and
its sha256 included. Three kinds of value are in play, and the test says which is which:

* **derived** — every key, the absence's lean-end set, the canonical spelling and key
  order, the record shapes, the receipt sidecars and their names, the seal's file list
  and shas, universe.json's sort order and display-name rule, and both orderings;
* **carried** — the capped quote and the span's sha256, which come from the article's
  text layer and are in no sealed set (``records.cut_passage`` is tested on a synthetic
  layer in test_records.py instead);
* **stated** — the seal's pins, which are an input to any export.

It runs on two things:

1. ``scripts/pipeline/fixtures/recordset/``, the pipeline's own synthetic set, always.
   The test also rebuilds it from ``make_fixture.py`` and refuses a difference, so the
   committed fixture cannot drift from the writer, and hands it to
   ``scripts/articles/build_store.py``, so what this producer writes is what WikiLean's
   consumer accepts.
2. Every directory under ``$WIKILEAN_LOCATE_SETS`` that holds a ``SEAL.json`` — a real
   batch, which is not committed (WikiLean is public: a record set quotes articles).
   Skipped when the variable is unset, so a fresh checkout runs green.

Run: python3 scripts/pipeline/test_record_sets.py
     WIKILEAN_LOCATE_SETS=<dir> python3 scripts/pipeline/test_record_sets.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))
sys.path.insert(0, str(ROOT / "scripts" / "articles"))

import build_store  # noqa: E402
import export_records  # noqa: E402
import keys  # noqa: E402
import links  # noqa: E402
import make_fixture  # noqa: E402
import readback  # noqa: E402
import records as records_mod  # noqa: E402
import status as status_mod  # noqa: E402
import vocab  # noqa: E402

FIXTURE = HERE / "fixtures" / "recordset"
REAL_SETS = os.environ.get("WIKILEAN_LOCATE_SETS", "")


def real_sets() -> list[Path]:
    if not REAL_SETS:
        return []
    root = Path(REAL_SETS).expanduser()
    if (root / "SEAL.json").is_file():
        return [root]
    return sorted(path for path in root.iterdir() if (path / "SEAL.json").is_file())


def tree(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes()
            for p in sorted(path.rglob("*")) if p.is_file()}


class SetChecks:
    """The checks run over one sealed set, whichever set it is."""

    def check_set(self, set_dir: Path) -> None:
        read = readback.read_set(set_dir)
        self.check_links(set_dir, read)
        self.check_keys(read)
        self.check_status(read)
        self.check_reexport(set_dir, read)

    # R1
    def check_links(self, set_dir: Path, read: readback.SealedSet) -> None:
        problems: list[str] = []
        count = 0
        for rel in sorted(read.seal["files"]):
            if not rel.startswith("records/"):
                continue
            for line in (set_dir / rel).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    count += 1
                    problems.extend(links.validate(json.loads(line)))
        self.assertGreater(count, 0)
        self.assertEqual(problems[:3], [], f"{len(problems)} records fail the vendored checker")

    # R2
    def check_keys(self, read: readback.SealedSet) -> None:
        if read.status_rule < vocab.STATUS_RULE_BUNDLE:
            return
        libraries = {str(lib["id"]): lib for lib in read.universe_view["libraries"]}
        for row in read.rows:
            cut = read.cut(row)
            source = read.source(row["source_id"])
            passage = keys.passage_key(source["pageid"], cut.span_sha256)
            self.assertEqual(row["passage_key"], passage, row["item"])
            for loc in row["locations"]:
                pair = keys.pair_key(passage, str(libraries[loc["library"]]["repo"]),
                                     str(loc["declaration"]))
                self.assertTrue(vocab.HEX64.match(pair))
            claim = row.get("coverage")
            if claim:
                pairs = {loc["location_key"]: keys.pair_key(
                    passage, str(libraries[loc["library"]]["repo"]), str(loc["declaration"]))
                    for loc in row["locations"]}
                self.assertEqual(claim["bundle_key"],
                                 keys.bundle_key(passage, [pairs[k] for k in claim["parts"]]),
                                 row["item"])

    # R3
    def check_status(self, read: readback.SealedSet) -> None:
        wrong = []
        for row in read.rows:
            if row["outcome"] == "unresolved":
                self.assertIsNone(row["status"])
                continue
            derived = status_mod.derive(row["passage_kind"], row["locations"],
                                        row["outcome"] == "absent", row.get("coverage"),
                                        read.status_rule)
            if derived != row["status"]:
                wrong.append((row["source_id"], row["item"], row["status"], derived))
        self.assertEqual(wrong[:5], [], f"{len(wrong)} passages carry another status than the "
                                        "rules derive")

    # R5 and R6
    def check_reexport(self, set_dir: Path, read: readback.SealedSet) -> None:
        seal = read.seal
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "set"
            report = export_records.build_set(
                rows=read.rows, sources=read.sources, universe=read.universe,
                set_id=seal["set_id"], campaign=seal["campaign"],
                sourceset=seal["sourceset"]["id"],
                sourceset_manifest=seal["sourceset"]["manifest_sha256"],
                producer_commit=seal["skill_commit"], created=seal["created"],
                status_rule=read.status_rule, byte_budget=seal["byte_budget"], out=out)
            self.assertEqual(report["refused"], [])
            self.assertEqual(report["exported"]["passages"], len(read.rows))
            self.assertEqual(report["seal_sha256"], read.seal_sha256)
            mine, theirs = tree(out), tree(set_dir)
            self.assertEqual(sorted(mine), sorted(theirs))
            differ = sorted(rel for rel in theirs if mine[rel] != theirs[rel])
            self.assertEqual(differ, [], f"{len(differ)} files differ from the sealed set")


class FixtureTests(unittest.TestCase, SetChecks):
    """The pipeline's own synthetic set: written here, read back here, built by the consumer."""

    def test_the_committed_fixture_is_what_the_maker_writes(self):
        # The maker refuses a target outside the fixtures directory (so the command line
        # can never put article text in a public repository), hence the scratch name here.
        staged = HERE / "fixtures" / ".scratch-recordset"
        try:
            report = make_fixture.write(staged)
            self.assertEqual(report["refused"], [])
            self.assertEqual(tree(staged), tree(FIXTURE))
        finally:
            shutil.rmtree(staged, ignore_errors=True)

    def test_the_fixture_is_a_status_rule_3_bundle_set(self):
        seal = json.loads((FIXTURE / "SEAL.json").read_text(encoding="utf-8"))
        self.assertEqual(seal["schema"], vocab.SEAL_SCHEMA_BUNDLE)
        self.assertEqual(seal["status_rule"], 3)
        self.assertEqual(sorted(seal), sorted(vocab.SEAL_KEYS_BUNDLE))

    def test_the_whole_chain(self):
        self.check_set(FIXTURE)

    def test_it_covers_every_outcome_and_shape(self):
        read = readback.read_set(FIXTURE)
        self.assertEqual({row["outcome"] for row in read.rows},
                         {"located", "absent", "unresolved"})
        self.assertEqual({row["prominence"] for row in read.rows},
                         set(vocab.PROMINENCES))
        challenges = {loc["challenge"] for row in read.rows for loc in row["locations"]}
        self.assertEqual(challenges, vocab.CHALLENGES - {"not_required"})
        relations = {loc["relation"] for row in read.rows for loc in row["locations"]}
        self.assertTrue({"exactMatch", "closeMatch", "broadMatch", "hasPart",
                         "relatedMatch"} <= relations)
        self.assertTrue(any(row.get("coverage") for row in read.rows))
        self.assertTrue(any("end_line" not in loc
                            for row in read.rows for loc in row["locations"]))

    def test_the_store_builder_accepts_what_this_producer_writes(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "store"
            done = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "articles" / "build_store.py"),
                 "--record-set", str(FIXTURE), "--out", str(out)],
                capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(done.returncode, 0, done.stderr)
            current = json.loads((out / "articles" / "v1" / "current.json").read_text("utf-8"))
            self.assertEqual(current["counts"]["articles"], 2)
            self.assertEqual(current["record_set"]["status_rule"], 3)

    def test_the_absence_names_a_split_repository_once(self):
        read = readback.read_set(FIXTURE)
        absent = [row for row in read.rows if row["outcome"] == "absent"]
        self.assertEqual(len(absent), 1)
        ends = records_mod.absence_ends(read.universe_view)
        # three libraries, two of them one repository at one commit
        self.assertEqual(len(read.universe_view["libraries"]), 3)
        self.assertEqual(len(ends), 2)


class ReadbackTests(unittest.TestCase):
    """What the reader refuses."""

    def copy(self, scratch: str) -> Path:
        out = Path(scratch) / "set"
        out.mkdir()
        for rel, data in tree(FIXTURE).items():
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return out

    def reseal(self, set_dir: Path) -> None:
        """Re-seal a changed copy, so the test is about the reader and not about the seal."""
        seal = json.loads((set_dir / "SEAL.json").read_text(encoding="utf-8"))
        seal["files"] = {rel: keys.sha256_file(set_dir / rel) for rel in sorted(seal["files"])}
        (set_dir / "SEAL.json").write_text(keys.canonical_line(seal), encoding="utf-8")

    def test_the_seal_is_checked_before_anything_is_read(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            target = out / "sources.jsonl"
            target.write_bytes(target.read_bytes() + b"\n")
            self.assertTrue(readback.verify_set(out))
            with self.assertRaises(readback.SetProblem) as caught:
                readback.read_set(out)
            self.assertIn("sha256 does not match the seal", str(caught.exception))

    def test_a_file_the_seal_does_not_name_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            (out / "extra.json").write_text("{}", encoding="utf-8")
            self.assertEqual(readback.verify_set(out), ["extra.json: not in the seal"])

    def test_a_missing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            (out / "universe.json").unlink()
            self.assertEqual(readback.verify_set(out), ["universe.json: missing"])

    def test_a_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            target = out / "universe.json"
            data = target.read_bytes()
            target.unlink()
            (out / "elsewhere.json").write_bytes(data)
            target.symlink_to("elsewhere.json")
            self.assertTrue(any("symlink" in problem for problem in readback.verify_set(out)))

    def test_an_unknown_schema_or_status_rule_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            seal = json.loads((out / "SEAL.json").read_text(encoding="utf-8"))
            for over in ({"schema": "cf-locate-recordset/9"}, {"status_rule": 1},
                         {"status_rule": "3"}):
                with self.subTest(over=over):
                    (out / "SEAL.json").write_text(keys.canonical_line({**seal, **over}),
                                                   encoding="utf-8")
                    with self.assertRaises(readback.SetProblem):
                        readback.read_set(out)

    def test_a_record_that_disagrees_with_its_source_is_refused(self):
        # A re-export writes the subject end from the source row, so a record that
        # disagreed with sources.jsonl would come back with other bytes and no reason.
        for key, value in (("renderer", "web-html/9"), ("html_sha256", "0" * 64),
                           ("pageid", 999)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as scratch:
                out = self.copy(scratch)
                rel = "records/enwiki-1001-2002.jsonl"
                lines = []
                for line in (out / rel).read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    record["subject"][0]["extensions"][vocab.LOCATE][key] = value
                    lines.append(keys.canonical(record))
                (out / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
                self.reseal(out)
                with self.assertRaises(readback.SetProblem) as caught:
                    readback.read_set(out)
                self.assertIn(f"the subject end's {key}", str(caught.exception))

    def test_a_subject_end_with_another_permalink_is_refused(self):
        # The permalink is derived from the family; a set spelling it another way is
        # named here rather than found as a byte difference after a re-export.
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            rel = "records/enwiki-1001-2002.jsonl"
            lines = []
            for line in (out / rel).read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                record["subject"][0]["uri"] = (
                    "https://en.wikipedia.org/w/index.php?title=Widget_lemma&oldid=2002")
                lines.append(keys.canonical(record))
            (out / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.reseal(out)
            with self.assertRaises(readback.SetProblem) as caught:
                readback.read_set(out)
            self.assertIn("the subject end's uri", str(caught.exception))

    def test_records_of_one_passage_that_disagree_are_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            rel = "records/enwiki-1001-2002.jsonl"
            lines = (out / rel).read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["extensions"][vocab.LOCATE]["prominence"] = "subject"
            lines[0] = keys.canonical(first)
            (out / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.reseal(out)
            with self.assertRaises(readback.SetProblem) as caught:
                readback.read_set(out)
            self.assertIn("disagree on prominence", str(caught.exception))

    def test_a_subject_end_without_a_span_is_refused_by_name(self):
        # A set from before the producer's split-frontend commit: its statuses can be
        # checked, but it cannot be re-exported, because nothing says where the passage is.
        with tempfile.TemporaryDirectory() as scratch:
            out = self.copy(scratch)
            rel = "records/enwiki-1003-2004.jsonl"
            lines = []
            for line in (out / rel).read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                ext = record["subject"][0]["extensions"][vocab.LOCATE]
                ext.pop("span", None)
                ext.pop("layer_sha256", None)
                lines.append(keys.canonical(record))
            (out / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.reseal(out)
            with self.assertRaises(readback.SetProblem) as caught:
                readback.read_set(out)
            self.assertIn("carries no span", str(caught.exception))

    def test_the_command_verifies_and_writes_rows(self):
        with tempfile.TemporaryDirectory() as scratch, io.StringIO() as quiet:
            rows = Path(scratch) / "rows.jsonl"
            with contextlib.redirect_stdout(quiet):
                self.assertEqual(readback.main(["--record-set", str(FIXTURE), "--verify-only"]), 0)
                self.assertEqual(readback.main(["--record-set", str(FIXTURE), "--out", str(rows)]), 0)
            written = [json.loads(line) for line in rows.read_text("utf-8").splitlines()]
            self.assertEqual(len(written), 8)
            self.assertEqual({row["schema"] for row in written}, {readback.ROW_SCHEMA})


class ExportTests(unittest.TestCase):
    """What the writer refuses before it writes anything, and what it writes."""

    def inputs(self) -> dict:
        return {"rows": make_fixture.rows(), "sources": make_fixture.sources(),
                "universe": make_fixture.UNIVERSE, "set_id": make_fixture.SET_ID,
                "campaign": make_fixture.CAMPAIGN, "sourceset": make_fixture.SOURCESET,
                "sourceset_manifest": make_fixture.SOURCESET_MANIFEST,
                "producer_commit": make_fixture.PRODUCER_COMMIT,
                "created": make_fixture.CREATED, "status_rule": make_fixture.STATUS_RULE}

    def write(self, scratch: str, **over) -> dict:
        return export_records.build_set(**{**self.inputs(), **over},
                                        out=Path(scratch) / "set")

    def test_a_bad_pin_is_refused_before_anything_is_written(self):
        for over in ({"set_id": "_draft"}, {"campaign": "run:"}, {"created": "2026-11-01"},
                     {"producer_commit": "nope"}, {"sourceset_manifest": "abc"},
                     {"status_rule": 4}, {"byte_budget": -1}):
            with self.subTest(over=over), tempfile.TemporaryDirectory() as scratch:
                with self.assertRaises(export_records.ExportProblem):
                    self.write(scratch, **over)
                self.assertFalse((Path(scratch) / "set").exists())

    def test_a_universe_the_consumer_would_refuse_is_refused_here(self):
        universe = make_fixture.UNIVERSE
        broken = [
            {**universe, "frozen": "October"},
            {**universe, "libraries": [{**universe["libraries"][0], "licence": None}]},
            {**universe, "libraries": [{**universe["libraries"][0], "repo": "github.com/a/b"}]},
            {**universe, "libraries": [{**universe["libraries"][0], "commit": "zz"}]},
            {**universe, "libraries": [universe["libraries"][0], universe["libraries"][0]]},
            {**universe, "excluded": [{"id": "x", "reason": ""}]},
        ]
        for value in broken:
            with self.subTest(universe=value.get("frozen")), tempfile.TemporaryDirectory() as s:
                with self.assertRaises(export_records.ExportProblem):
                    self.write(s, universe=value)

    def test_one_root_of_one_repository_in_two_libraries_is_refused(self):
        universe = {**make_fixture.UNIVERSE,
                    "libraries": [make_fixture.UNIVERSE["libraries"][1],
                                  {**make_fixture.UNIVERSE["libraries"][2], "roots": ["Init"]}]}
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(export_records.ExportProblem) as caught:
                self.write(scratch, universe=universe)
            self.assertIn("is in two libraries", str(caught.exception))

    def test_the_display_name_is_derived_from_brain_lib(self):
        doc = export_records.universe_json(make_fixture.UNIVERSE)
        self.assertEqual([lib["display_name"] for lib in doc["libraries"]],
                         ["Init", "Std", "WidgetLib"])
        hidden = {**make_fixture.UNIVERSE,
                  "libraries": [{**make_fixture.UNIVERSE["libraries"][0],
                                 "brain_lib": "Widget​lib"}]}
        self.assertIsNone(export_records.universe_json(hidden)["libraries"][0]["display_name"])

    def test_a_row_that_cannot_be_written_is_refused_and_the_rest_are_kept(self):
        rows = [dict(row) for row in make_fixture.rows()]
        rows[0] = {**rows[0], "prominence": "headline"}
        with tempfile.TemporaryDirectory() as scratch:
            report = self.write(scratch, rows=rows)
            self.assertEqual(len(report["refused"]), 1)
            self.assertEqual(report["refused"][0]["item"], rows[0]["item"])
            self.assertEqual(report["exported"]["passages"], len(rows) - 1)

    def test_a_source_over_the_byte_budget_is_refused_whole(self):
        with tempfile.TemporaryDirectory() as scratch:
            report = self.write(scratch, byte_budget=1000)
            self.assertTrue(report["refused"])
            self.assertTrue(all(case["item"] is None for case in report["refused"]))
            self.assertIn("over the per-source budget", report["refused"][0]["reason"])

    def test_the_target_is_refused_when_it_is_not_an_earlier_set(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "set"
            out.mkdir()
            (out / "notes.txt").write_text("hello", encoding="utf-8")
            with self.assertRaises(export_records.ExportProblem):
                export_records.check_target(out)

    def test_the_checkout_is_refused_as_a_target(self):
        with self.assertRaises(export_records.ExportProblem) as caught:
            export_records.check_target(ROOT / "site" / "out" / "set")
        self.assertIn("inside this checkout", str(caught.exception))
        # the one exception: the synthetic fixture this pipeline builds
        export_records.check_target(HERE / "fixtures" / ".scratch", in_checkout=True)

    def test_a_re_export_removes_what_the_previous_seal_named(self):
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "set"
            self.write(scratch)
            self.assertTrue((out / "records" / "enwiki-1003-2004.jsonl").is_file())
            rows = [row for row in make_fixture.rows()
                    if row["source_id"] == "enwiki-1001-2002"]
            export_records.build_set(**{**self.inputs(), "rows": rows}, out=out)
            self.assertFalse((out / "records" / "enwiki-1003-2004.jsonl").is_file())
            self.assertEqual(readback.verify_set(out), [])

    def test_the_export_is_a_function_of_its_inputs(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = self.write(one), self.write(two)
            self.assertEqual(first["seal_sha256"], second["seal_sha256"])
            self.assertEqual(tree(Path(one) / "set"), tree(Path(two) / "set"))

    def test_the_report_names_the_checker_that_passed_the_set(self):
        with tempfile.TemporaryDirectory() as scratch:
            report = self.write(scratch)
            pin = links.pin()
            self.assertEqual(report["links_version"], pin["version"])
            self.assertEqual(report["links_schema_sha256"], pin["sha256"])


@unittest.skipUnless(REAL_SETS, "set WIKILEAN_LOCATE_SETS to a directory of sealed sets")
class RealSetTests(unittest.TestCase, SetChecks):
    """Every sealed set under $WIKILEAN_LOCATE_SETS, read back and re-exported.

    Not committed, and not needed for a green checkout: a real record set quotes
    articles, and this repository is public.
    """

    def test_every_set(self):
        found = real_sets()
        self.assertTrue(found, f"no sealed set under {REAL_SETS}")
        for set_dir in found:
            with self.subTest(set=set_dir.name):
                self.check_set(set_dir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
