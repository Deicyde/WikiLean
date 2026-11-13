#!/usr/bin/env python3
"""Tests for upload_contrib.py: an index bundle into the contributor bucket.

New code. Every test packages a small synthetic bundle with scripts/pipeline/package_index.py
and uploads it to test_upload_store's loopback S3 server, which checks each request's SigV4
signature, payload hash and Content-MD5. No network, no real credential. What is checked:

* the keys are the bundle's content address, ``index/v1/<sha256 of MANIFEST.json>/<path>``,
  and MANIFEST.json goes up last;
* a key is never replaced: one holding other bytes, or one under the prefix the bundle does
  not list, is refused before anything is written, and a second run uploads nothing;
* a bundle not fit to hand out is refused: no licence texts, a library with no licence
  recorded, or use restrictions not acknowledged exactly;
* a file that differs from the manifest, or one the manifest does not list, is refused
  before any request;
* an upload whose ETag is not the MD5 stops before MANIFEST.json;
* the credentials are the contributor bucket's own variables, and no output carries them.

Run: python3 scripts/articles/test_upload_contrib.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "pipeline"))

import package_index  # noqa: E402
import upload_contrib  # noqa: E402
from test_upload_store import ACCESS_KEY_ID, SECRET_ACCESS_KEY, FakeS3  # noqa: E402

BUCKET = "wikilean-contrib"
ENV = {upload_contrib.ACCESS_KEY_VAR: ACCESS_KEY_ID, upload_contrib.SECRET_KEY_VAR: SECRET_ACCESS_KEY}

UNIVERSE = {
    "id": "u-test",
    "frozen": "2026-01-01",
    "universe_sha256": "a" * 64,
    "libraries": [
        {"id": "alpha", "repo": "https://github.com/example/alpha",
         "commit": "1" * 40, "licence": {"spdx": "Apache-2.0"}},
        {"id": "gamma", "repo": "https://github.com/example/gamma",
         "commit": "3" * 40, "licence": {"spdx": "CC-BY-NC-4.0"}},
    ],
}
SEARCH = {
    "declarations.tsv": "alpha\tAlpha.foo\ttheorem\tproved\tAlpha.Foo\tAlpha/Foo.lean:3\n",
    "signatures.tsv": "alpha\tAlpha.foo\ttheorem Alpha.foo : 1 = 1\n",
    "docstrings.tsv": "alpha\tAlpha.foo\tA foo.\n",
    "wikidata.tsv": "alpha\tAlpha.foo\tQ42\n",
    "README.md": "# the search corpus\n",
}
INDEX_LINES = (
    '{"schema":"wikilean.pipeline.index/1","universe":"u-test","universe_sha256":'
    f'"{"a" * 64}"}}\n'
    '{"commit":"1111111111111111111111111111111111111111","declaration":"Alpha.foo",'
    '"library":"alpha","proof_status":"proved"}\n')
TEXTS = {"alpha": {"LICENSE": "Apache License 2.0\n", "NOTICE": "alpha notice\n"},
         "gamma": {"LICENSE.md": "CC BY-NC 4.0\n"}}


class ContribCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="contrib-upload-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.s3 = FakeS3(bucket=BUCKET)
        self.addCleanup(self.s3.close)
        self.outputs: list[str] = []

    def tearDown(self) -> None:
        for text in self.outputs:
            self.assertNotIn(ACCESS_KEY_ID, text)
            self.assertNotIn(SECRET_ACCESS_KEY, text)
        self.assertEqual(self.s3.refusals, [], "the fake bucket refused a request's signature or digest")

    def bundle(self, name: str = "bundle", universe: dict | None = None,
               texts: dict | None = TEXTS) -> Path:
        work = self.tmp / f"{name}-inputs"
        (work / "search").mkdir(parents=True)
        (work / "universe.json").write_text(json.dumps(universe or UNIVERSE), encoding="utf-8")
        (work / "decl-index.jsonl").write_text(INDEX_LINES, encoding="utf-8")
        for file, text in SEARCH.items():
            (work / "search" / file).write_text(text, encoding="utf-8")
        licences = None
        if texts is not None:
            licences = work / "licences"
            for library, files in texts.items():
                (licences / library).mkdir(parents=True)
                for file, text in files.items():
                    (licences / library / file).write_text(text, encoding="utf-8")
        out = self.tmp / name
        with redirect_stdout(io.StringIO()):
            package_index.package(index=work / "decl-index.jsonl", search=work / "search",
                                  universe=work / "universe.json", out=out,
                                  created="2026-11-13T00:00:00Z", licences=licences)
        return out

    def run_upload(self, bundle: Path, *options: str, env: dict | None = None) -> tuple[int, str, str]:
        argv = ["bundle", str(bundle), "--bucket", BUCKET, "--endpoint", self.s3.endpoint, *options]
        out, err = io.StringIO(), io.StringIO()
        code = upload_contrib.main(argv, environ=ENV if env is None else env, stdout=out, stderr=err,
                                   sleep=lambda _seconds: None)
        self.outputs.append(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def prefix(bundle: Path) -> str:
        sha = hashlib.sha256((bundle / "MANIFEST.json").read_bytes()).hexdigest()
        return f"index/v1/{sha}/"

    def puts(self) -> list[str]:
        return [key for method, key in self.s3.log if method == "PUT"]


class UploadTests(ContribCase):
    def test_a_dry_run_reads_the_prefix_and_writes_nothing(self):
        bundle = self.bundle()
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.puts(), [])
        self.assertEqual([method for method, _ in self.s3.log], ["GET"])
        self.assertIn("dry run", out)
        self.assertIn("upload  index/decl-index.jsonl", out)
        self.assertIn(self.prefix(bundle), out)

    def test_every_file_goes_under_the_manifest_s_sha256_and_the_manifest_last(self):
        bundle = self.bundle()
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 0, out + err)
        prefix = self.prefix(bundle)
        manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
        expected = {prefix + rel for rel in manifest["files"]} | {prefix + "MANIFEST.json"}
        self.assertEqual(set(self.s3.objects), expected)
        self.assertEqual(self.puts()[-1], prefix + "MANIFEST.json")
        for key, data in self.s3.objects.items():
            self.assertEqual(data, (bundle / key[len(prefix):]).read_bytes(), key)
        self.assertIn(f"{prefix}licences/alpha/LICENSE", self.s3.objects)
        self.assertIn("none missing, none extra", out)

    def test_a_second_run_uploads_nothing(self):
        bundle = self.bundle()
        self.assertEqual(self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")[0], 0)
        self.s3.log.clear()
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.puts(), [])
        self.assertIn("0 uploaded", out)

    def test_a_key_holding_other_bytes_is_refused_and_nothing_is_written(self):
        bundle = self.bundle()
        self.s3.put_object(self.prefix(bundle) + "README.md", b"somebody else's readme\n")
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 2, out + err)
        self.assertIn("never replaced", err)
        self.assertEqual(self.puts(), [])

    def test_an_object_the_bundle_does_not_list_under_its_prefix_is_refused(self):
        bundle = self.bundle()
        self.s3.put_object(self.prefix(bundle) + "stray.txt", b"?")
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 2, out + err)
        self.assertIn("does not list", err)
        self.assertEqual(self.puts(), [])

    def test_an_etag_that_is_not_the_md5_stops_before_the_manifest(self):
        bundle = self.bundle()
        prefix = self.prefix(bundle)
        self.s3.wrong_etag.add(prefix + "index/decl-index.jsonl")
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 1, out + err)
        self.assertNotIn(prefix + "MANIFEST.json", self.s3.objects)


class RefusalTests(ContribCase):
    def test_restrictions_must_be_acknowledged_exactly(self):
        bundle = self.bundle()
        for options in ((), ("--acknowledge-restricted", "alpha"),
                        ("--acknowledge-restricted", "gamma", "--acknowledge-restricted", "alpha")):
            with self.subTest(options=options):
                code, out, err = self.run_upload(bundle, *options, "--execute")
                self.assertEqual(code, 2, out + err)
                self.assertIn("restricted", err)
        self.assertEqual(self.puts(), [])

    def test_a_bundle_without_its_licence_texts_is_refused(self):
        bundle = self.bundle(texts=None)
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 2, out + err)
        self.assertIn("licence texts", err)
        self.assertEqual(self.s3.log, [])

    def test_a_library_with_no_licence_recorded_is_refused(self):
        universe = json.loads(json.dumps(UNIVERSE))
        universe["libraries"].append({"id": "beta", "repo": "https://github.com/example/beta",
                                      "commit": "2" * 40})
        bundle = self.bundle(universe=universe, texts={**TEXTS, "beta": {"COPYING": "?\n"}})
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 2, out + err)
        self.assertIn("beta", err)
        self.assertEqual(self.s3.log, [])

    def test_a_file_that_differs_from_the_manifest_is_refused_before_any_request(self):
        bundle = self.bundle()
        (bundle / "search" / "declarations.tsv").write_text("changed\n", encoding="utf-8")
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 2, out + err)
        self.assertIn("search/declarations.tsv", err)
        self.assertEqual(self.s3.log, [])

    def test_a_file_the_manifest_does_not_list_is_refused(self):
        bundle = self.bundle()
        (bundle / "notes.txt").write_text("mine\n", encoding="utf-8")
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", "--execute")
        self.assertEqual(code, 2, out + err)
        self.assertIn("notes.txt", err)
        self.assertEqual(self.s3.log, [])

    def test_an_older_bundle_schema_is_refused(self):
        bundle = self.bundle()
        manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
        manifest["schema"] = "wikilean.pipeline.index-bundle/1"
        (bundle / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma")
        self.assertEqual(code, 2, out + err)
        self.assertIn("index-bundle/2", err)

    def test_a_path_that_would_need_encoding_or_leaves_the_bundle_is_refused(self):
        for rel in ("a b.tsv", "../x", ".hidden", "x//y", "/abs", "-x", "MANIFEST.json", 7):
            with self.subTest(rel=rel), self.assertRaises(upload_contrib.Refused):
                upload_contrib.listed_path(rel)
        self.assertEqual(upload_contrib.listed_path("licences/atlas-v2/LICENSE.md"),
                         "licences/atlas-v2/LICENSE.md")


class CredentialTests(ContribCase):
    def test_the_articles_bucket_s_variables_are_not_read(self):
        bundle = self.bundle()
        env = {"WIKILEAN_ARTICLES_R2_ACCESS_KEY_ID": ACCESS_KEY_ID,
               "WIKILEAN_ARTICLES_R2_SECRET_ACCESS_KEY": SECRET_ACCESS_KEY}
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma", env=env)
        self.assertEqual(code, 2, out + err)
        self.assertIn(upload_contrib.ACCESS_KEY_VAR, err)
        self.assertEqual(self.s3.log, [])

    def test_a_0600_file_with_the_contrib_names_works(self):
        bundle = self.bundle()
        path = self.tmp / "contrib.env"
        path.write_text(f"{upload_contrib.ACCESS_KEY_VAR}={ACCESS_KEY_ID}\n"
                        f"export {upload_contrib.SECRET_KEY_VAR}='{SECRET_ACCESS_KEY}'\n", encoding="utf-8")
        os.chmod(path, 0o600)
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma",
                                         "--credentials-file", str(path), env={})
        self.assertEqual(code, 0, out + err)
        os.chmod(path, 0o644)
        code, out, err = self.run_upload(bundle, "--acknowledge-restricted", "gamma",
                                         "--credentials-file", str(path), env={})
        self.assertEqual(code, 2, out + err)
        self.assertIn("0600", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
