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
        summary = article["summary"]
        self.assertEqual(summary["by_status"], {"formalized": 1, "statement_only": 1, "partial": 0,
                                                "not_formalized": 1, "unresolved": 1})
        self.assertEqual(summary["by_library"]["batteries"], {"locations": 0, "passages": 0, "related": 1})
        self.assertEqual(summary["by_library"]["mathlib"], {"locations": 1, "passages": 1, "related": 0})
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
        self.assertEqual(shard["articles"], [["Widget lemma", 1001, 2002, 4, 1, 1, 0, 1, 1, "high"]])
        self.assertEqual(shard["fields"][0:3], ["title", "pageid", "revision"])


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
        self.refuse("needs an exact/close/broad match to a proved declaration")

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
