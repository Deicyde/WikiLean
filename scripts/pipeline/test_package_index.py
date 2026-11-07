#!/usr/bin/env python3
"""Tests for the index bundle: the manifest, the licences and the names-only subset.

New code. Everything runs against a synthetic universe and a four-line search corpus
written into a temporary directory — no corpus database, no network, no real index,
and no library's real licence. What is checked:

* the bundle's shape and that **every file in it is in the manifest with its sha256**,
  which is the only thing a contributor who downloads it can check;
* **the names-only subset carries no Lean source text**: it is `declarations.tsv` and
  `wikidata.tsv` and nothing else, and the two files that do carry source text are
  named as such in the manifest;
* **a licence the universe does not record is `NOASSERTION` and is listed**, never
  silently treated as permission;
* **a use restriction is found from all three places it can come from** — the SPDX id,
  the universe's own exclusion list naming the same repository, and the operator — and
  the command exits non-zero when there is one;
* the bundle is a function of its inputs: two runs over the same files write the same
  manifest.

Run: python3 scripts/pipeline/test_package_index.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import package_index  # noqa: E402

UNIVERSE = {
    "id": "u-test",
    "frozen": "2026-01-01",
    "universe_sha256": "a" * 64,
    "libraries": [
        {"id": "alpha", "repo": "https://github.com/example/alpha",
         "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}},
        {"id": "beta", "repo": "https://github.com/example/beta", "commit": "2" * 40},
        {"id": "gamma", "repo": "https://github.com/example/gamma",
         "commit": "3" * 40, "licence": {"spdx": "CC-BY-NC-4.0"}},
    ],
    "excluded": [
        {"id": "alpha-v1",
         "reason": "Alpha v1 (v1/ of example/alpha): CC BY-NC licence with a "
                   "no-ML-evaluation rider"},
        {"id": "delta", "reason": "a private repository"},
    ],
}

SEARCH = {
    "declarations.tsv": "alpha\tAlpha.foo\ttheorem\tproved\tAlpha.Foo\tAlpha/Foo.lean:3\n",
    "signatures.tsv": "alpha\tAlpha.foo\ttheorem Alpha.foo : 1 = 1\n",
    "docstrings.tsv": "alpha\tAlpha.foo\tA foo, which is a widget with a handle.\n",
    "wikidata.tsv": "alpha\tAlpha.foo\tQ42\n",
    "README.md": "# the search corpus\n",
}

INDEX_LINES = (
    '{"schema":"wikilean.pipeline.index/1","universe":"u-test","universe_sha256":'
    f'"{"a" * 64}"}}\n'
    '{"commit":"1111111111111111111111111111111111111111","declaration":"Alpha.foo",'
    '"library":"alpha","proof_status":"proved"}\n')

CREATED = "2026-11-06T00:00:00Z"


def quiet(call, *args, **kwargs):
    with io.StringIO() as sink, redirect_stdout(sink):
        return call(*args, **kwargs)


class BundleCase(unittest.TestCase):
    """One synthetic universe, index and search corpus in a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wl-index-bundle-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.universe = self.tmp / "universe.json"
        self.universe.write_text(json.dumps(UNIVERSE), encoding="utf-8")
        self.index = self.tmp / "decl-index.jsonl"
        self.index.write_text(INDEX_LINES, encoding="utf-8")
        self.search = self.tmp / "search"
        self.search.mkdir()
        for name, text in SEARCH.items():
            (self.search / name).write_text(text, encoding="utf-8")

    def package(self, out="bundle", **kwargs):
        options = {"index": self.index, "search": self.search,
                   "universe": self.universe, "out": self.tmp / out,
                   "created": CREATED}
        options.update(kwargs)
        return package_index.package(**options)


class TestBundle(BundleCase):
    def test_every_file_is_in_the_manifest_with_its_sha256(self):
        manifest = self.package()
        out = self.tmp / "bundle"
        on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*")
                   if p.is_file() and p.name != package_index.MANIFEST_NAME}
        self.assertEqual(on_disk, set(manifest["files"]))
        for rel, entry in manifest["files"].items():
            self.assertEqual(package_index.sha256_file(out / rel), entry["sha256"], rel)
            self.assertEqual((out / rel).stat().st_size, entry["bytes"], rel)
        self.assertEqual(manifest["schema"], package_index.BUNDLE_SCHEMA)
        self.assertEqual(manifest["bundle"], f"u-test/{manifest['version']}")
        self.assertFalse(manifest["uploaded"])

    def test_the_index_and_the_whole_search_corpus_travel(self):
        self.package()
        out = self.tmp / "bundle"
        self.assertEqual((out / "index" / "decl-index.jsonl").read_text(encoding="utf-8"),
                         INDEX_LINES)
        for name, text in SEARCH.items():
            self.assertEqual((out / "search" / name).read_text(encoding="utf-8"), text)

    def test_the_names_only_subset_carries_no_lean_source_text(self):
        manifest = self.package()
        out = self.tmp / "bundle"
        names = {p.name for p in (out / "names").iterdir()}
        self.assertEqual(names, set(package_index.NAMES_FILES) | {"README.md"})
        body = "".join((out / "names" / name).read_text(encoding="utf-8")
                       for name in package_index.NAMES_FILES)
        # the signature and the docstring are the source text, and neither is here
        self.assertNotIn("theorem Alpha.foo : 1 = 1", body)
        self.assertNotIn("A foo, which is a widget", body)
        self.assertIn("Alpha.foo", body)
        self.assertIn("Q42", body)
        self.assertEqual(manifest["source_text_files"],
                         ["search/signatures.tsv", "search/docstrings.tsv"])

    def test_two_runs_over_one_input_write_one_manifest(self):
        first = self.package(out="a")
        second = self.package(out="b")
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(first["version"], second["version"])
        # the whole manifest, and the two licence files it names, byte for byte: the
        # bundle carries no path of the machine that wrote it and no clock of its own
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        for name in (package_index.LICENCES_JSON, package_index.LICENCES_MD):
            self.assertEqual((self.tmp / "a" / name).read_bytes(),
                             (self.tmp / "b" / name).read_bytes(), name)

    def test_the_version_is_derived_from_the_universe_and_the_index(self):
        manifest = self.package()
        self.assertEqual(manifest["version"],
                         "a" * 12 + "-" + manifest["index"]["sha256"][:12])
        named = self.package(out="named", version="u1-2026-11")
        self.assertEqual(named["version"], "u1-2026-11")

    def test_a_rewritten_index_gets_a_new_version(self):
        before = self.package()["version"]
        self.index.write_text(INDEX_LINES + '{"library":"alpha"}\n', encoding="utf-8")
        self.assertNotEqual(self.package(out="again")["version"], before)


class TestLicences(BundleCase):
    def test_a_library_with_no_licence_is_noassertion_and_is_listed(self):
        manifest = self.package()
        spdx = json.loads((self.tmp / "bundle" / package_index.LICENCES_JSON)
                          .read_text(encoding="utf-8"))
        self.assertEqual(spdx["no_licence_recorded"], ["beta"])
        self.assertEqual(manifest["licences"]["no_licence_recorded"], ["beta"])
        beta = next(p for p in spdx["packages"] if p["name"] == "beta")
        self.assertEqual(beta["licenseDeclared"], package_index.NOASSERTION)
        self.assertFalse(beta["wikilean"]["restricted"])
        self.assertIn("`beta`", (self.tmp / "bundle" / package_index.LICENCES_MD)
                      .read_text(encoding="utf-8"))

    def test_one_spdx_record_per_library_of_the_universe(self):
        self.package()
        spdx = json.loads((self.tmp / "bundle" / package_index.LICENCES_JSON)
                          .read_text(encoding="utf-8"))
        self.assertEqual([p["name"] for p in spdx["packages"]],
                         ["alpha", "beta", "gamma"])
        alpha = next(p for p in spdx["packages"] if p["name"] == "alpha")
        self.assertEqual(alpha["licenseDeclared"], "Apache-2.0")
        # nothing was scanned here, so nothing is concluded here
        self.assertEqual(alpha["licenseConcluded"], package_index.NOASSERTION)
        self.assertFalse(alpha["filesAnalyzed"])
        self.assertEqual(alpha["versionInfo"], "1" * 40)

    def test_a_noncommercial_spdx_id_is_a_restriction(self):
        self.package()
        spdx = json.loads((self.tmp / "bundle" / package_index.LICENCES_JSON)
                          .read_text(encoding="utf-8"))
        gamma = next(p for p in spdx["packages"] if p["name"] == "gamma")
        self.assertTrue(gamma["wikilean"]["restricted"])
        self.assertEqual([r["source"] for r in gamma["wikilean"]["restrictions"]],
                         ["spdx"])

    def test_an_exclusion_for_a_licence_flags_the_same_repository(self):
        """u1's own case: v1 of a repository is excluded for its licence, v2 is in."""
        self.package()
        spdx = json.loads((self.tmp / "bundle" / package_index.LICENCES_JSON)
                          .read_text(encoding="utf-8"))
        alpha = next(p for p in spdx["packages"] if p["name"] == "alpha")
        self.assertTrue(alpha["wikilean"]["restricted"])
        reasons = alpha["wikilean"]["restrictions"]
        self.assertEqual([r["source"] for r in reasons], ["universe.excluded"])
        self.assertIn("no-ML-evaluation", reasons[0]["reason"])

    def test_an_exclusion_that_is_not_about_a_licence_flags_nothing(self):
        """`delta` is excluded for being private, which says nothing about use."""
        self.package()
        spdx = json.loads((self.tmp / "bundle" / package_index.LICENCES_JSON)
                          .read_text(encoding="utf-8"))
        self.assertEqual(sorted(spdx["use_restricted"]), ["alpha", "gamma"])

    def test_a_shared_owner_is_not_the_same_repository(self):
        """`example/alpha` and `example/beta` share an owner and nothing else."""
        loose = dict(UNIVERSE)
        loose["excluded"] = [{"id": "zeta", "repo": "https://github.com/example/zeta",
                              "reason": "CC BY-NC, no ML evaluation"}]
        self.universe.write_text(json.dumps(loose), encoding="utf-8")
        rows = {row["id"]: row for row in package_index.library_licences(loose)}
        self.assertEqual([name for name, row in rows.items() if row["restricted"]],
                         ["gamma"])  # gamma's own SPDX, and nothing by repository

    def test_a_shared_leading_token_is_not_a_version(self):
        """One excluded `ff-*` library must not flag every other `ff-*` library."""
        wide = {
            "id": "u", "universe_sha256": "a" * 64,
            "libraries": [{"id": f"ff-{name}",
                           "repo": f"https://github.com/FF/{name}",
                           "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}}
                          for name in ("one", "two", "three")],
            "excluded": [{"id": "ff-incubator",
                          "reason": "CC BY-NC with a no-ML-evaluation rider"}],
        }
        rows = package_index.library_licences(wide)
        self.assertEqual([row["id"] for row in rows if row["restricted"]], [])

    def test_a_versioned_sibling_is_a_version(self):
        self.assertTrue(package_index._versioned_sibling("atlas", "atlas-v1"))
        self.assertTrue(package_index._versioned_sibling("atlas-v12", "atlas"))
        self.assertTrue(package_index._versioned_sibling("atlas-v1", "atlas-v2"))
        self.assertFalse(package_index._versioned_sibling("ff-one", "ff-two"))
        self.assertFalse(package_index._versioned_sibling("atlas", "atlas-lean"))
        self.assertFalse(package_index._versioned_sibling("", "atlas-v1"))
        self.assertFalse(package_index._versioned_sibling("atlas", "atlas"))

    def test_a_repository_is_the_same_repository_however_its_url_is_spelled(self):
        for url in ("https://github.com/example/alpha",
                    "https://github.com/example/alpha/",
                    "https://github.com/example/alpha.git",
                    "git@github.com:example/alpha.git"):
            with self.subTest(url=url):
                self.assertEqual(package_index._repo_of_url(url), "example/alpha")
        # a bare host is not a repository, and neither is <host>/<owner>
        self.assertEqual(package_index._repo_of_url("https://github.com/example"), "")
        self.assertEqual(package_index._repo_of_url("https://github.com"), "")
        self.assertEqual(package_index._repo_of_url(None), "")

    def test_a_dot_git_suffix_does_not_hide_a_restriction(self):
        suffixed = dict(UNIVERSE)
        suffixed["libraries"] = [
            {"id": "alpha", "repo": "https://github.com/example/alpha.git",
             "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}}]
        suffixed["excluded"] = [
            {"id": "alpha-old", "repo": "https://github.com/example/alpha",
             "reason": "CC BY-NC with a no-ML-evaluation rider"}]
        row, = package_index.library_licences(suffixed)
        self.assertTrue(row["restricted"])

    def test_a_reason_that_names_the_repository_only_as_a_url_still_joins(self):
        by_url = dict(UNIVERSE)
        by_url["libraries"] = [
            {"id": "alpha", "repo": "https://github.com/example/alpha",
             "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}},
            {"id": "beta", "repo": "https://github.com/example/beta",
             "commit": "2" * 40, "licence": {"spdx": "Apache-2.0"}}]
        by_url["excluded"] = [
            {"id": "alpha-old",
             "reason": "non-commercial licence, see https://github.com/example/alpha"}]
        rows = {row["id"]: row for row in package_index.library_licences(by_url)}
        self.assertTrue(rows["alpha"]["restricted"])
        # and the URL's own <host>/<owner> does not drag beta in with it
        self.assertFalse(rows["beta"]["restricted"])

    def test_a_reason_that_only_says_proprietary_is_still_a_restriction(self):
        for reason in ("proprietary licence, all rights reserved",
                       "the licence forbids redistribution",
                       "copyright holder has not agreed"):
            with self.subTest(reason=reason):
                wide = dict(UNIVERSE)
                wide["libraries"] = [
                    {"id": "alpha", "repo": "https://github.com/example/alpha",
                     "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}}]
                wide["excluded"] = [{"id": "alpha-v1",
                                     "repo": "https://github.com/example/alpha",
                                     "reason": reason}]
                row, = package_index.library_licences(wide)
                self.assertTrue(row["restricted"], reason)

    def test_a_bare_string_licence_is_read_rather_than_called_noassertion(self):
        bare = dict(UNIVERSE)
        bare["libraries"] = [{"id": "alpha", "repo": "https://example.invalid/a",
                              "commit": "1" * 40, "licence": "CC-BY-NC-4.0"}]
        bare["excluded"] = []
        row, = package_index.library_licences(bare)
        self.assertEqual(row["spdx"], "CC-BY-NC-4.0")
        self.assertTrue(row["restricted"])

    def test_an_odd_universe_shape_does_not_crash(self):
        odd = {"id": "u", "universe_sha256": "a" * 64, "excluded": None,
               "libraries": [{"id": "a"}, {"id": "b", "licence": None, "repo": None},
                             {"id": 7, "licence": {"spdx": None}}, "not a library"]}
        rows = package_index.library_licences(odd)
        self.assertEqual([row["id"] for row in rows], ["7", "a", "b"])
        self.assertTrue(all(row["spdx"] == package_index.NOASSERTION for row in rows))

    def test_a_versioned_sibling_is_flagged_without_a_repository_to_join_on(self):
        """u1's shape: the exclusion records an id and a reason, and no repo field."""
        quiet_universe = {
            "id": "u", "universe_sha256": "a" * 64,
            "libraries": [{"id": "atlas", "repo": "https://example.invalid/elsewhere",
                           "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}}],
            "excluded": [{"id": "atlas-v1", "reason": "CC BY-NC, no ML evaluation"}],
        }
        row, = package_index.library_licences(quiet_universe)
        self.assertTrue(row["restricted"])
        self.assertIn("another version of the same library",
                      row["restrictions"][0]["reason"])
        self.assertIn("atlas-v1", row["restrictions"][0]["reason"])

    def test_the_operator_may_state_a_restriction(self):
        self.package(out="stated", restricted={"beta": "the vendor said no evaluation"})
        spdx = json.loads((self.tmp / "stated" / package_index.LICENCES_JSON)
                          .read_text(encoding="utf-8"))
        beta = next(p for p in spdx["packages"] if p["name"] == "beta")
        self.assertTrue(beta["wikilean"]["restricted"])
        self.assertEqual(beta["wikilean"]["restrictions"][0]["source"], "operator")

    def test_a_restriction_on_a_library_the_universe_does_not_have_is_refused(self):
        with self.assertRaises(package_index.PackageProblem) as caught:
            self.package(out="nope", restricted={"epsilon": "no"})
        self.assertIn("epsilon", str(caught.exception))
        self.assertFalse((self.tmp / "nope").exists())

    def test_the_markdown_table_names_every_library_and_every_flag(self):
        self.package()
        text = (self.tmp / "bundle" / package_index.LICENCES_MD).read_text(encoding="utf-8")
        for name in ("alpha", "beta", "gamma"):
            self.assertIn(f"`{name}`", text)
        self.assertIn("Libraries whose use is restricted", text)
        self.assertIn("Libraries with no licence recorded", text)
        self.assertIn("Lean source text", text)


class TestRefusals(BundleCase):
    def test_half_a_search_corpus_is_refused_before_anything_is_written(self):
        (self.search / "signatures.tsv").unlink()
        with self.assertRaises(package_index.PackageProblem) as caught:
            self.package(out="half")
        self.assertIn("signatures.tsv", str(caught.exception))
        self.assertFalse((self.tmp / "half").exists())

    def test_a_provenance_of_another_index_is_refused(self):
        provenance = self.tmp / "prov.json"
        provenance.write_text(json.dumps({"index": {"sha256": "f" * 64}}),
                              encoding="utf-8")
        with self.assertRaises(package_index.PackageProblem) as caught:
            self.package(out="wrong", provenance=provenance)
        self.assertIn("another index", str(caught.exception))
        self.assertFalse((self.tmp / "wrong").exists())

    def test_the_converter_s_own_provenance_rides_in_the_manifest(self):
        provenance = self.tmp / "prov.json"
        provenance.write_text(json.dumps(
            {"index": {"sha256": package_index.sha256_file(self.index), "rows": 1},
             "rows": 1, "libraries": 1}), encoding="utf-8")
        manifest = self.package(out="withprov", provenance=provenance)
        self.assertEqual(manifest["converted"]["rows"], 1)

    def test_a_non_empty_directory_that_is_not_a_bundle_is_refused(self):
        target = self.tmp / "occupied"
        target.mkdir()
        (target / "something.txt").write_text("hi", encoding="utf-8")
        with self.assertRaises(package_index.PackageProblem) as caught:
            self.package(out="occupied")
        self.assertIn("not empty", str(caught.exception))
        self.assertTrue((target / "something.txt").is_file())

    def test_a_half_written_bundle_can_be_written_again(self):
        """A copy a full disk interrupted must not need a manual rm -rf to retry."""
        target = self.tmp / "partial"
        (target / package_index.INDEX_DIR).mkdir(parents=True)
        (target / package_index.INDEX_DIR / "decl-index.jsonl").write_text(
            "half of it", encoding="utf-8")
        manifest = self.package(out="partial")
        self.assertTrue(manifest["files"])
        self.assertEqual(
            (target / "index" / "decl-index.jsonl").read_text(encoding="utf-8"),
            INDEX_LINES)

    def test_a_second_run_does_not_leave_the_first_run_s_files_behind(self):
        """A renamed index must not ship twice, with the stale one sha256-vouched."""
        self.package(out="twice")
        renamed = self.tmp / "decl-index-v2.jsonl"
        renamed.write_text(INDEX_LINES, encoding="utf-8")
        manifest = self.package(out="twice", index=renamed)
        self.assertIn("index/decl-index-v2.jsonl", manifest["files"])
        self.assertNotIn("index/decl-index.jsonl", manifest["files"])
        self.assertFalse((self.tmp / "twice" / "index" / "decl-index.jsonl").exists())

    def test_packaging_a_bundle_over_its_own_input_is_refused(self):
        self.package(out="bundle")
        inner = self.tmp / "bundle" / "index" / "decl-index.jsonl"
        with self.assertRaises(package_index.PackageProblem) as caught:
            self.package(out="bundle", index=inner,
                         search=self.tmp / "bundle" / "search")
        self.assertIn("inside", str(caught.exception))
        # and the input it would have eaten is still there
        self.assertEqual(inner.read_text(encoding="utf-8"), INDEX_LINES)

    def test_a_provenance_that_is_not_one_is_refused_rather_than_crashing(self):
        for value in ({"index": "decl-index.jsonl"}, ["not", "a", "document"],
                      {"index": {"sha256": 7}}):
            provenance = self.tmp / "odd.json"
            provenance.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), \
                    self.assertRaises(package_index.PackageProblem):
                self.package(out="odd", provenance=provenance)

    def test_a_bad_created_time_is_refused(self):
        with self.assertRaises(package_index.PackageProblem):
            self.package(out="clock", created="yesterday")

    def test_a_universe_without_libraries_is_refused(self):
        self.universe.write_text(json.dumps({"id": "u"}), encoding="utf-8")
        with self.assertRaises(package_index.PackageProblem) as caught:
            self.package(out="nolibs")
        self.assertIn("not a universe document", str(caught.exception))


class TestCommandLine(BundleCase):
    def argv(self, out: str, *extra: str) -> list[str]:
        return ["--index", str(self.index), "--search", str(self.search),
                "--universe", str(self.universe), "--out", str(self.tmp / out),
                "--created", CREATED, *extra]

    def test_it_exits_non_zero_when_a_licence_restricts_use(self):
        self.assertEqual(quiet(package_index.main, self.argv("cli")), 1)

    def test_it_exits_zero_when_every_licence_is_recorded_and_free(self):
        clean = dict(UNIVERSE)
        clean["libraries"] = [UNIVERSE["libraries"][1] | {"licence": {"spdx": "MIT"}}]
        clean["excluded"] = []
        self.universe.write_text(json.dumps(clean), encoding="utf-8")
        self.assertEqual(quiet(package_index.main, self.argv("clean")), 0)

    def test_it_prints_the_sizes_and_the_flags(self):
        with io.StringIO() as sink, redirect_stdout(sink):
            package_index.main(self.argv("printed"))
            text = sink.getvalue()
        self.assertIn("names/declarations.tsv", text)
        self.assertIn("RESTRICTED: alpha", text)
        self.assertIn("NOASSERTION: beta", text)
        self.assertIn("nothing was uploaded", text)

    def test_restricted_takes_library_equals_reason(self):
        self.assertEqual(quiet(package_index.main,
                               self.argv("pair", "--restricted", "beta=no evaluation")), 1)
        with self.assertRaises(package_index.PackageProblem):
            package_index._restricted_pairs(["beta"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
